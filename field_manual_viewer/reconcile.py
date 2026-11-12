"""Offline, bidirectional reconciliation between the archive and the catalog.

Physical PDF and EPUB files are authoritative for local availability.  The
database remains the source of catalog enrichment and search acceleration and
can be rebuilt completely from the archive when it is missing.
"""
import datetime
import errno
import hashlib
import json
import os
import re
import stat as stat_module
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from pathlib import Path

from . import core
from .core import FileLock, database, load_json, save_json
from .indexing import ENGINE

GOOD = ('complete', 'skipped_verified', 'recovered')
UNSUPPORTED_MARKERS = ('.part', '.partial', '.tmp', '.temp', '.rejected',
                       '.corrupt-', '.crdownload', '.download', '.filepart')
SUPPORTED = {'.pdf': 'pdf', '.epub': 'epub'}
SAFE_ID = re.compile(r'[A-Za-z0-9_.-]{1,120}\Z')
SCAN_INTERVAL = 8.0
RETRY_DELAY = 0.5

_STATE_LOCK = threading.RLock()
_WORKER = None
_LAST_FINISHED = 0.0


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _report_path():
    return Path(core.ROOT) / 'reports/reconcile.json'


def _write_report(**values):
    current = load_json(_report_path(), {})
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    current['updated_utc'] = _utcnow()
    current['offline'] = True
    current['network_used'] = False
    save_json(_report_path(), current)
    return current


def report():
    data = load_json(_report_path(), {})
    if not isinstance(data, dict):
        data = {}
    running = bool(_WORKER and _WORKER.is_alive())
    data.setdefault('state', 'idle')
    data.setdefault('message', 'Library check has not run yet.')
    data['active'] = running
    if running and data.get('state') == 'idle':
        data['message'] = 'Checking the archive for new or changed documents.'
    if not running and data.get('state') in ('scanning', 'verifying', 'indexing'):
        # A crash or forced shutdown can leave an active-looking progress file.
        data['state'] = 'idle'
        data['message'] = 'Library check is not running.'
    data['worker_running'] = running
    data.setdefault('revision', _read_revision())
    data['offline'] = True
    data['network_used'] = False
    return data


def _read_revision():
    try:
        with closing(database()) as db:
            row = db.execute("SELECT value FROM reconcile_state WHERE key='revision'").fetchone()
            return int(row[0]) if row and str(row[0]).isdigit() else 0
    except Exception:
        return 0


def _bump_revision(db):
    row = db.execute("SELECT value FROM reconcile_state WHERE key='revision'").fetchone()
    revision = (int(row[0]) if row and str(row[0]).isdigit() else 0) + 1
    db.execute("INSERT OR REPLACE INTO reconcile_state VALUES ('revision',?)", (str(revision),))
    return revision


def _archive_root():
    return (Path(core.ROOT) / 'archive').resolve()


def _archive_available():
    root = _archive_root()
    try:
        if not root.is_dir():
            return False
        with os.scandir(root) as entries:
            next(entries, None)
        return True
    except OSError:
        return False


def _ignored_name(name):
    lower = name.lower()
    return (not name or name.startswith('.') or name.startswith('~$')
            or any(marker in lower for marker in UNSUPPORTED_MARKERS))


def _iter_archive():
    """Yield regular PDF/EPUB files without following links outside archive."""
    root = _archive_root()
    if not root.is_dir():
        return
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        path = Path(entry.path)
                        fmt = SUPPORTED.get(path.suffix.lower())
                        if not fmt or _ignored_name(path.name):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if not stat_module.S_ISREG(info.st_mode) or info.st_size <= 0:
                            continue
                        resolved = path.resolve()
                        if not resolved.is_relative_to(root):
                            continue
                        rel = resolved.relative_to(Path(core.ROOT).resolve()).as_posix()
                        yield rel, fmt, info
                    except (OSError, ValueError):
                        continue
        except OSError:
            continue


def _discovered():
    return sorted(((rel, fmt, {'bytes': info.st_size, 'mtime_ns': info.st_mtime_ns})
                   for rel, fmt, info in _iter_archive()),
                  key=lambda item: (item[0].casefold(), item[1]))


def _norm_path(value):
    return str(value or '').replace('\\', '/')


def _path_key(value):
    normalized = _norm_path(value)
    return normalized.casefold() if os.name == 'nt' else normalized


def _humanize(value):
    value = re.sub(r'[_\-]+', ' ', str(value or ''))
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:200]


def _candidate_identity(rel, fmt, claimed, known_ids):
    parts = Path(rel).parts
    name = parts[-1] if parts else rel
    stem = Path(name).stem
    folder = parts[-2] if len(parts) >= 3 else None
    if (folder and SAFE_ID.fullmatch(folder)
            and folder.lower() not in ('archive', 'pdf', 'pdfs', 'epub', 'epubs',
                                       'documents', 'docs')
            and (folder, fmt) not in claimed):
        canonical = stem == folder
        return folder, stem, 'canonical' if canonical else 'folder'
    # A stable identity derived from the relative path.  It survives rescans,
    # library moves and restarts, and cannot collide with a catalog identity.
    slug = re.sub(r'[^A-Za-z0-9]+', '-', stem).strip('-').lower()[:48] or 'document'
    digest = hashlib.sha256(rel.encode('utf8')).hexdigest()[:10]
    base = f'local-{slug}-{digest}'
    candidate, counter = base, 2
    while (candidate, fmt) in claimed or candidate in known_ids:
        candidate = f'{base}-{counter}'
        counter += 1
    return candidate, stem, 'path'


def _publication_url(p, fmt):
    url = p.get(fmt + '_url')
    if url:
        return url
    for asset in p.get('assets', []) or []:
        if isinstance(asset, dict) and asset.get('format') == fmt and asset.get('url'):
            return asset['url']
    return None


def _manifest_records():
    """Use a surviving manifest or per-edition metadata as offline enrichment."""
    records = {}
    manifest = load_json(Path(core.ROOT) / 'data/manifest.json', [])
    if isinstance(manifest, list):
        for publication in manifest:
            if isinstance(publication, dict) and publication.get('id'):
                records[publication['id']] = publication
    for path in sorted((Path(core.ROOT) / 'data/metadata').glob('*.json')):
        try:
            publication = json.loads(path.read_text(encoding='utf8'))
        except (OSError, ValueError):
            continue
        if isinstance(publication, dict) and publication.get('id'):
            records[publication['id']] = publication
    return records


def _new_publication(pub_id, stem, source, manifest=None):
    if manifest:
        p = dict(manifest)
        p['id'] = pub_id
        p['_reconstructed'] = True
        p['_recovered_from_manifest'] = True
        p['_recovered_from_archive'] = True
        p['_provenance'] = sorted(set(p.get('_provenance', [])) | {'surviving-manifest'})
        p.setdefault('_stub', False)
        p.setdefault('_metadata_level', 'surviving-manifest')
        return p
    title = pub_id if source in ('canonical', 'folder') else _humanize(stem)
    return {'id': pub_id, 'title': title or pub_id, '_stub': False,
            '_reconstructed': True, '_metadata_level': 'reconstructed',
            '_title_source': 'identifier' if source in ('canonical', 'folder') else 'filename',
            '_provenance': ['archive-scan'], '_recovered_from_archive': True}


def _ensure_publication(db, pub_id, stem, source, manifest=None):
    row = db.execute('SELECT metadata FROM publications WHERE id=?', (pub_id,)).fetchone()
    if not row:
        p = _new_publication(pub_id, stem, source, manifest)
        db.execute('INSERT INTO publications VALUES (?,?)', (pub_id, json.dumps(p)))
        return p, True
    p = json.loads(row[0])
    changed = False
    if not p.get('title') and manifest and manifest.get('title'):
        p['title'] = manifest['title']
        p['_title_source'] = 'surviving-manifest'
        p['_provenance'] = sorted(set(p.get('_provenance', [])) | {'surviving-manifest'})
        changed = True
    elif not p.get('title'):
        p['title'] = (pub_id if source in ('canonical', 'folder')
                      else _humanize(stem)) or pub_id
        p['_title_source'] = 'identifier' if source in ('canonical', 'folder') else 'filename'
        changed = True
    if changed:
        db.execute('UPDATE publications SET metadata=? WHERE id=?', (json.dumps(p), pub_id))
    return p, changed


def quick_sync(write_report=True):
    """Register added files and presence changes without hashing anything."""
    now = _utcnow()
    archive_available = _archive_available()
    discovered = _discovered() if archive_available else []
    changed = False
    new_records = 0
    revision = None
    with closing(database()) as db, db:
        known_rows = [dict(r) for r in db.execute('SELECT * FROM files')]
        known_by_path = {_path_key(row['path']): row for row in known_rows}
        claimed = {(row['pub_id'], row['format']) for row in known_rows}
        known_ids = {row[0] for row in db.execute('SELECT id FROM publications')}
        presences = {(r['pub_id'], r['format']): dict(r)
                     for r in db.execute('SELECT * FROM file_presence')}
        evidences = {(r['pub_id'], r['format']): dict(r)
                     for r in db.execute('SELECT * FROM file_evidence')}
        manifest = None
        for row in known_rows:
            if row['pub_id'] in known_ids:
                continue
            path = Path(row['path'])
            stem = path.stem
            source = 'canonical' if path.parent.name == stem else 'path'
            if manifest is None:
                manifest = _manifest_records()
            p = _new_publication(row['pub_id'], stem, source, manifest.get(row['pub_id']))
            db.execute('INSERT INTO publications VALUES (?,?)', (row['pub_id'], json.dumps(p)))
            known_ids.add(row['pub_id'])
            changed = True
        seen = set()
        for rel, fmt, info in discovered:
            seen.add(_path_key(rel))
            row = known_by_path.get(_path_key(rel))
            if row:
                if row['path'] != rel:
                    # Older libraries or hand-edited databases may use
                    # backslashes.  Keep one portable relative spelling.
                    db.execute('UPDATE files SET path=? WHERE pub_id=? AND format=?',
                               (rel, row['pub_id'], row['format']))
                    row['path'] = rel
                    changed = True
                key = (row['pub_id'], row['format'])
                presence = presences.get(key)
                stamp = evidences.get(key)
                last_good = (row['status'] in GOOD and row['sha256'] and stamp
                             and stamp.get('sha256') == row['sha256']
                             and stamp.get('mtime_ns') is not None)
                desired = None
                if last_good:
                    # Keep the last verified state visible while a changed file is
                    # rechecked.  The reader boundary still validates the real file.
                    desired = (1, row['bytes'], stamp['mtime_ns'])
                elif presence is None or not presence['present']:
                    desired = (1, info['bytes'], info['mtime_ns'])
                if desired and (not presence or not presence['present']
                                or presence['bytes'] != desired[1]
                                or presence['mtime_ns'] != desired[2]):
                    changed = True
                    db.execute('''INSERT OR REPLACE INTO file_presence
                                  (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
                                  VALUES (?,?,?,?,?,?,?,?,?)''',
                               (row['pub_id'], row['format'], 1, desired[1], desired[2], now,
                                None if last_good else (presence['error'] if presence else None),
                                None if last_good else (presence['error_bytes'] if presence else None),
                                None if last_good else (presence['error_mtime_ns'] if presence else None)))
                    presences[key] = {'pub_id': row['pub_id'], 'format': row['format'],
                                      'present': 1, 'bytes': desired[1],
                                      'mtime_ns': desired[2]}
                continue
            pub_id, stem, source = _candidate_identity(rel, fmt, claimed, known_ids)
            claimed.add((pub_id, fmt))
            known_ids.add(pub_id)
            if manifest is None:
                manifest = _manifest_records()
            p, _ = _ensure_publication(db, pub_id, stem, source, manifest.get(pub_id))
            db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)',
                       (pub_id, fmt, _publication_url(p, fmt), rel, 'pending_verification',
                        info['bytes'], None, None, now))
            db.execute('''INSERT OR REPLACE INTO file_presence
                          (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
                          VALUES (?,?,?,?,?,?,?,?,?)''',
                       (pub_id, fmt, 1, info['bytes'], info['mtime_ns'], now, None, None, None))
            new_records += 1
            changed = True
        for row in known_rows:
            if not archive_available:
                # A disconnected or unreadable archive is not the same as hundreds
                # of individually deleted files. Keep the last known presence.
                continue
            if _path_key(row['path']) in seen:
                continue
            key = (row['pub_id'], row['format'])
            presence = presences.get(key)
            if presence is None or presence['present']:
                changed = True
                db.execute('''INSERT OR REPLACE INTO file_presence
                              (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
                              VALUES (?,?,?,?,?,?,?,?,?)''',
                           (row['pub_id'], row['format'], 0, row['bytes'],
                            presence['mtime_ns'] if presence else None, now,
                            presence['error'] if presence else None,
                            presence['error_bytes'] if presence else None,
                            presence['error_mtime_ns'] if presence else None))
        if changed:
            revision = _bump_revision(db)
        else:
            revision = _read_revision_from(db)
    if write_report:
        _write_report(state='scanning', active=True,
                      message='Checking the archive for new or changed documents.',
                      started_utc=now, last_scan_utc=now, revision=revision,
                      archive_available=archive_available,
                      discovered=len(discovered), registered=len(known_rows) + new_records,
                      new_documents=new_records)
    return {'discovered': len(discovered), 'new': new_records,
            'registered': len(known_rows) + new_records, 'changed': changed,
            'revision': revision, 'archive_available': archive_available}


def _read_revision_from(db):
    row = db.execute("SELECT value FROM reconcile_state WHERE key='revision'").fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else 0


def _validated_path(relative):
    path = (Path(core.ROOT) / relative).resolve()
    if not path.is_relative_to(_archive_root()):
        raise ValueError('Original path is outside the archive')
    return path


def _verification_candidates(deep=False):
    with closing(database()) as db:
        rows = [dict(r) for r in db.execute('''
            SELECT f.*, e.sha256 AS evidence_sha, e.bytes AS evidence_bytes,
                   e.mtime_ns AS evidence_mtime,
                   p.present AS presence_present, p.bytes AS presence_bytes,
                   p.mtime_ns AS presence_mtime, p.error AS presence_error,
                   p.error_bytes AS presence_error_bytes,
                   p.error_mtime_ns AS presence_error_mtime
            FROM files f
            LEFT JOIN file_evidence e ON e.pub_id=f.pub_id AND e.format=f.format
            LEFT JOIN file_presence p ON p.pub_id=f.pub_id AND p.format=f.format
        ''')]
    candidates = []
    for row in rows:
        try:
            info = _validated_path(row['path']).stat()
        except (OSError, ValueError):
            continue
        if not stat_module.S_ISREG(info.st_mode) or info.st_size <= 0:
            continue
        good = row['status'] in GOOD and bool(row['sha256'])
        if not deep and good and (row['evidence_sha'] == row['sha256']
                                  and row['evidence_bytes'] == info.st_size
                                  and row['evidence_mtime'] == info.st_mtime_ns):
            continue
        if (not deep and row['status'] == 'damaged' and not good and row['presence_error']
                and row['presence_error_bytes'] == info.st_size
                and row['presence_error_mtime'] == info.st_mtime_ns):
            # The same invalid bytes were already diagnosed.  A later size or
            # timestamp change queues the file again automatically.
            continue
        candidates.append(row)
    return candidates


def _mark_verified(row, sha, info):
    now = _utcnow()
    with closing(database()) as db, db:
        db.execute('''UPDATE files SET status=?, error=NULL, bytes=?, sha256=?, updated=?
                      WHERE pub_id=? AND format=?''',
                   ('recovered', info.st_size, sha, now, row['pub_id'], row['format']))
        db.execute('INSERT OR REPLACE INTO file_evidence VALUES (?,?,?,?,?)',
                   (row['pub_id'], row['format'], sha, info.st_size, info.st_mtime_ns))
        db.execute('''INSERT OR REPLACE INTO file_presence
                      (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
                      VALUES (?,?,?,?,?,?,?,?,?)''',
                   (row['pub_id'], row['format'], 1, info.st_size, info.st_mtime_ns, now,
                    None, None, None))
        return _bump_revision(db)


def _mark_damaged(row, info, error):
    now = _utcnow()
    message = str(error)[:500]
    with closing(database()) as db, db:
        db.execute('''UPDATE files SET status=?, error=?, bytes=?, updated=?
                      WHERE pub_id=? AND format=?''',
                   ('damaged', message, info.st_size, now, row['pub_id'], row['format']))
        db.execute('''INSERT OR REPLACE INTO file_presence
                      (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
                      VALUES (?,?,?,?,?,?,?,?,?)''',
                   (row['pub_id'], row['format'], 1, info.st_size, info.st_mtime_ns, now,
                    message, info.st_size, info.st_mtime_ns))
        return _bump_revision(db)


def _mark_missing(row, error):
    now = _utcnow()
    with closing(database()) as db, db:
        db.execute('''INSERT OR REPLACE INTO file_presence
                      (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
                      VALUES (?,?,?,?,?,?,?,?,?)''',
                   (row['pub_id'], row['format'], 0, row['bytes'], None, now,
                    str(error)[:500], None, None))
        return _bump_revision(db)


def _embedded_metadata(path, fmt):
    try:
        if fmt == 'pdf':
            from pypdf import PdfReader
            meta = PdfReader(str(path)).metadata or {}
            return {key: str(meta.get(source) or '').strip()
                    for key, source in (('title', '/Title'), ('creator', '/Author'),
                                        ('date', '/CreationDate'))}
        with zipfile.ZipFile(path) as archive:
            container_data = archive.read('META-INF/container.xml')
            if b'<!entity' in container_data.lower():
                raise ValueError('EPUB XML entity declarations are not allowed')
            container = ET.fromstring(container_data)
            rootfile = next((e for e in container.iter() if e.tag.endswith('rootfile')), None)
            if rootfile is None or not rootfile.attrib.get('full-path'):
                return {}
            package_data = archive.read(rootfile.attrib['full-path'])
            if b'<!entity' in package_data.lower():
                raise ValueError('EPUB XML entity declarations are not allowed')
            package = ET.fromstring(package_data)
            values = {}
            for element in package.iter():
                local = element.tag.rsplit('}', 1)[-1].lower()
                text = (element.text or '').strip()
                if not text:
                    continue
                if local == 'title' and not values.get('title'):
                    values['title'] = text[:300]
                elif local in ('creator', 'author') and not values.get('creator'):
                    values['creator'] = text[:300]
                elif local == 'date' and not values.get('date'):
                    values['date'] = text[:100]
                elif local == 'identifier' and not values.get('identifier'):
                    values['identifier'] = text[:300]
            return values
    except Exception:
        return {}


def _enrich_metadata(pub_id, path, fmt):
    metadata = _embedded_metadata(path, fmt)
    if not any(metadata.values()):
        return
    with closing(database()) as db, db:
        row = db.execute('SELECT metadata FROM publications WHERE id=?', (pub_id,)).fetchone()
        if not row:
            return
        p = json.loads(row[0])
        changed = False
        if metadata.get('title') and (not p.get('title') or p.get('_title_source') in ('identifier', 'filename')):
            p['title'] = metadata['title']
            p['_title_source'] = 'embedded'
            changed = True
        document = dict(p.get('_document_metadata') or {})
        for key in ('creator', 'date', 'identifier'):
            if metadata.get(key) and not document.get(key):
                document[key] = metadata[key]
                changed = True
        if document and document != p.get('_document_metadata'):
            p['_document_metadata'] = document
            changed = True
        if changed:
            p['_provenance'] = sorted(set(p.get('_provenance', [])) | {'embedded-document-metadata'})
            db.execute('UPDATE publications SET metadata=? WHERE id=?', (json.dumps(p), pub_id))
            _bump_revision(db)


def _transient_file_error(error):
    if isinstance(error, (PermissionError, TimeoutError)):
        return True
    if not isinstance(error, OSError):
        return False
    if getattr(error, 'winerror', None) in (5, 32, 33, 145):
        return True
    return getattr(error, 'errno', None) in (errno.EACCES, errno.EBUSY, errno.EAGAIN, errno.ETIMEDOUT)


def _verify_one(row):
    pub_id, fmt = row['pub_id'], row['format']
    try:
        path = _validated_path(row['path'])
        before = path.stat()
    except FileNotFoundError as error:
        _mark_missing(row, str(error))
        return {'status': 'missing', 'error': str(error)}
    except (OSError, ValueError) as error:
        if _transient_file_error(error):
            return {'status': 'deferred', 'error': str(error)}
        _mark_missing(row, str(error))
        return {'status': 'missing', 'error': str(error)}
    if not stat_module.S_ISREG(before.st_mode) or before.st_size <= 0:
        _mark_missing(row, 'Original is not a regular non-empty file')
        return {'status': 'missing', 'error': 'Original is not a regular non-empty file'}
    try:
        from .collect import validate
        sha = validate(path, fmt)
        after = path.stat()
        if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            return {'status': 'deferred', 'error': 'Original is still being copied.'}
    except Exception as error:
        time.sleep(RETRY_DELAY)
        try:
            after = path.stat()
        except OSError as missing:
            _mark_missing(row, str(missing))
            return {'status': 'missing', 'error': str(missing)}
        if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            return {'status': 'deferred', 'error': 'Original is still being copied.'}
        if _transient_file_error(error):
            return {'status': 'deferred', 'error': str(error)}
        _mark_damaged(row, before, str(error))
        return {'status': 'damaged', 'error': str(error)}
    _mark_verified(row, sha, after)
    try:
        _enrich_metadata(pub_id, path, fmt)
    except Exception:
        # Enrichment is optional; a verified original remains readable.
        pass
    return {'status': 'verified', 'sha256': sha}


def _pending_index_pub_ids():
    with closing(database()) as db:
        return [row[0] for row in db.execute('''
            SELECT DISTINCT f.pub_id FROM files f
            LEFT JOIN index_state i ON i.pub_id=f.pub_id AND i.format=f.format
            LEFT JOIN file_presence p ON p.pub_id=f.pub_id AND p.format=f.format
            WHERE f.status IN ('complete','skipped_verified','recovered')
              AND f.sha256 IS NOT NULL AND COALESCE(p.present,1)=1
              AND (i.pub_id IS NULL OR i.error IS NOT NULL
                   OR i.sha256 != f.sha256 OR i.engine != ?)
            ORDER BY f.pub_id
        ''', (ENGINE,))]


def _index_publication(pub_id, ignore_setup=False):
    if not ignore_setup and _setup_active():
        return {'status': 'deferred'}
    try:
        from .indexing import index_one
        return index_one(pub_id)
    except Exception as error:
        return {'status': 'failed', 'error': str(error)}


def _bump_catalog_revision():
    try:
        with closing(database()) as db, db:
            return _bump_revision(db)
    except Exception:
        return None


def _setup_active():
    for name in ('setup-job', 'acquisition'):
        try:
            with FileLock(name):
                pass
        except RuntimeError:
            return True
    return False


def _notify(progress, message):
    if progress:
        try:
            progress(message)
        except Exception:
            pass


def _check_pause(ignore_setup):
    if ignore_setup and (Path(core.ROOT) / 'data/pause-acquisition').exists():
        from .collect import CampaignPaused
        raise CampaignPaused('Paused; saved originals and reconciliation progress are retained.')


def _process_sync(index=True, ignore_setup=False, deep=False, progress=None):
    started = _utcnow()
    _notify(progress, 'Checking the archive for new or changed documents.')
    _write_report(state='scanning', active=True, started_utc=started,
                  message='Checking the archive for new or changed documents.',
                  verified_this_run=0, indexed_this_run=0, damaged_this_run=0,
                  deferred_this_run=0, finished_utc=None)
    summary = quick_sync(write_report=False)
    revision = summary['revision']
    _check_pause(ignore_setup)
    if not ignore_setup and _setup_active():
        _write_report(state='deferred', active=False,
                      message='Library preparation is active. Archive recovery will resume after it finishes.',
                      revision=revision)
        return report()
    if not summary['archive_available']:
        _write_report(state='idle', active=False, revision=revision,
                      message='Archive folder is unavailable. Showing the last known catalog; no records were changed.',
                      finished_utc=_utcnow(), archive_available=False,
                      discovered=summary['discovered'], registered=summary['registered'],
                      new_documents=summary['new'], pending_verification=0)
        return report()

    candidates = _verification_candidates(deep=deep)
    total = len(candidates)
    stats = {'verified': 0, 'damaged': 0, 'deferred': 0, 'indexed': 0, 'index_errors': 0}
    for number, row in enumerate(candidates, 1):
        _check_pause(ignore_setup)
        if not ignore_setup and _setup_active():
            _write_report(state='deferred', active=False,
                          message='Library preparation is active. Archive recovery will resume after it finishes.',
                          revision=revision, verified_this_run=stats['verified'],
                          indexed_this_run=stats['indexed'], damaged_this_run=stats['damaged'],
                          deferred_this_run=stats['deferred'])
            return report()
        _notify(progress, f'Checking saved documents: {number:,} of {total:,}.')
        _write_report(state='verifying', active=True, revision=revision,
                      message=f'Checking saved documents: {number:,} of {total:,}.',
                      current=row['path'], pending_verification=max(0, total - number + 1),
                      verified_this_run=stats['verified'], indexed_this_run=stats['indexed'],
                      damaged_this_run=stats['damaged'], deferred_this_run=stats['deferred'])
        result = _verify_one(row)
        if result['status'] == 'verified':
            stats['verified'] += 1
            revision = _read_revision()
            if index:
                _notify(progress, f'Preparing {row["pub_id"]} for search.')
                _write_report(state='indexing', active=True, revision=revision,
                              message='Preparing verified documents for search.',
                              current=row['path'], verified_this_run=stats['verified'],
                              indexed_this_run=stats['indexed'], damaged_this_run=stats['damaged'],
                              deferred_this_run=stats['deferred'])
                results = _index_publication(row['pub_id'], ignore_setup)
                if isinstance(results, list) and any(r.get('status') == 'indexed' for r in results):
                    stats['indexed'] += 1
                    _bump_catalog_revision()
                elif isinstance(results, list) and any(r.get('status') == 'failed' for r in results):
                    stats['index_errors'] += 1
                elif isinstance(results, dict) and results.get('status') == 'failed':
                    stats['index_errors'] += 1
        elif result['status'] == 'damaged':
            stats['damaged'] += 1
            revision = _read_revision()
        elif result['status'] == 'deferred':
            stats['deferred'] += 1
            revision = _read_revision()
        time.sleep(.01)

    if index:
        pending = _pending_index_pub_ids()
        for number, pub_id in enumerate(pending, 1):
            _check_pause(ignore_setup)
            if not ignore_setup and _setup_active():
                _write_report(state='deferred', active=False,
                              message='Library preparation is active. Archive recovery will resume after it finishes.',
                              revision=revision, verified_this_run=stats['verified'],
                              indexed_this_run=stats['indexed'], damaged_this_run=stats['damaged'])
                return report()
            _notify(progress, f'Preparing search: {number:,} of {len(pending):,}.')
            _write_report(state='indexing', active=True, revision=revision,
                          message=f'Preparing search: {number:,} of {len(pending):,}.',
                          current=pub_id, verified_this_run=stats['verified'],
                          indexed_this_run=stats['indexed'], damaged_this_run=stats['damaged'],
                          deferred_this_run=stats['deferred'])
            results = _index_publication(pub_id, ignore_setup)
            if isinstance(results, list) and any(r.get('status') == 'indexed' for r in results):
                stats['indexed'] += 1
                _bump_catalog_revision()
            elif isinstance(results, list) and any(r.get('status') == 'failed' for r in results):
                stats['index_errors'] += 1
            elif isinstance(results, dict) and results.get('status') == 'failed':
                stats['index_errors'] += 1
            time.sleep(.01)

    revision = _read_revision()
    if stats['verified'] or stats['indexed']:
        message = (f'Library check complete. {stats["verified"]:,} document'
                   f'{"" if stats["verified"] == 1 else "s"} verified'
                   + (f', {stats["indexed"]:,} prepared for search.' if stats['indexed'] else '.'))
    elif stats['damaged']:
        message = ('Library check complete. Some files need attention; '
                   'their catalog records and search indexes were retained.')
    else:
        message = 'Library check complete. Everything is up to date.'
    _notify(progress, message)
    _write_report(state='idle', active=False, revision=revision, message=message,
                  finished_utc=_utcnow(), verified_this_run=stats['verified'],
                  indexed_this_run=stats['indexed'], damaged_this_run=stats['damaged'],
                  deferred_this_run=stats['deferred'], index_errors=stats['index_errors'],
                  archive_available=True,
                  discovered=summary['discovered'], registered=summary['registered'],
                  new_documents=summary['new'], pending_verification=stats['deferred'])
    return report()


def run_sync(index=True, ignore_setup=False, deep=False, wait_seconds=20, progress=None):
    """Run one complete reconciliation pass in the calling thread."""
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        try:
            with FileLock('reconcile'):
                return _process_sync(index=index, ignore_setup=ignore_setup, deep=deep,
                                     progress=progress)
        except RuntimeError as error:
            if 'Another Field Manual Viewer reconcile' not in str(error):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(.2)


def ensure_started(force=False):
    """Start one background pass; concurrent refreshes coalesce onto it."""
    global _WORKER
    with _STATE_LOCK:
        if _WORKER and _WORKER.is_alive():
            return False
        if not force and time.monotonic() - _LAST_FINISHED < SCAN_INTERVAL:
            return False
        if _setup_active():
            return False
        _WORKER = threading.Thread(target=_worker_loop, name='library-reconciliation', daemon=True)
        _WORKER.start()
        return True


def _worker_loop():
    global _WORKER, _LAST_FINISHED
    try:
        try:
            with FileLock('reconcile'):
                _process_sync(index=True)
        except RuntimeError:
            pass
        except Exception as error:
            try:
                _write_report(state='error', active=False,
                              message='Library check stopped: ' + str(error),
                              error=str(error))
            except Exception:
                pass
    finally:
        with _STATE_LOCK:
            _WORKER = None
            _LAST_FINISHED = time.monotonic()
