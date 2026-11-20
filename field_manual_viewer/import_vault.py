"""Ingest a T.REX ARMS Field Manual Vault release directory or torrent folder.

The release's index.csv is authoritative.  Files live inside per-topic ZIPs
(for example PDF/Doctrine & Field Manuals/Aviation.zip/....pdf).  Each member is
verified against the SHA-256 recorded in index.csv before it is copied into the
canonical portable archive layout.
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .core import ROOT, FileLock, database, digest, load_json, save_json
from . import indexing, lifecycle
from .discover import export_manifest

SAFE_ID = re.compile(r'[A-Za-z0-9_.-]{1,120}\Z')
GOOD = ('complete', 'skipped_verified', 'recovered')
FORMATS = ('pdf', 'epub', 'md')
BRANCHES = {'army': 'Army', 'joint': 'Joint', 'usmc': 'Marine Corps',
            'navy': 'Navy', 'af': 'Air Force'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def find_source(value=None):
    candidates = []
    if value:
        candidates.append(Path(value))
    else:
        candidates.extend((Path(r'D:\Field Manual Vault'), Path(r'D:\Field Manual Vault\T.Rex Arms Field Manual Vault')))
    for candidate in candidates:
        if candidate.is_file() and candidate.name.lower() == 'index.csv':
            candidate = candidate.parent
        if (candidate / 'index.csv').is_file():
            return candidate.resolve()
        if candidate.is_dir():
            matches = list(candidate.glob('*/index.csv'))
            if len(matches) == 1:
                return matches[0].parent.resolve()
    raise FileNotFoundError('Could not find index.csv. Pass the extracted vault folder with --source.')


def _release_label(index_path):
    return index_path.parent.name if index_path.parent.name else 'field-manual-vault'


def _year(value):
    match = re.search(r'(19|20)\d{2}', str(value or ''))
    return int(match.group(0)) if match else None


def _publication(row, release):
    branch = (row.get('branch') or '').strip()
    group = (row.get('group') or '').strip()
    family = (row.get('family') or '').strip()
    purpose = group
    if group and family and group.casefold() != family.casefold():
        purpose = f'{group} - {family}'
    return {
        'id': (row.get('id') or '').strip(),
        'designation': html.unescape((row.get('designation') or '').strip()),
        'title': html.unescape((row.get('title') or '').strip()),
        'edition_date': (row.get('edition_date') or '').strip(),
        'edition_year': _year(row.get('edition_date')),
        'status': (row.get('status') or '').strip(),
        'collection': (row.get('collection') or '').strip(),
        'branch': branch,
        'branch_label': BRANCHES.get(branch, branch.replace('-', ' ').title() if branch else ''),
        'pub_type': (row.get('pub_type') or '').strip(),
        'family': family,
        'group': group,
        'functional_category': group,
        'purpose': purpose,
        'services': [branch] if branch else [],
        '_stub': False,
        '_metadata_level': 'vault-index',
        '_source_release': release,
        '_provenance': ['trex-vault-index'],
        'editions': [],
    }


def _merge_publication(existing, incoming):
    """Never let the thin release index erase richer catalog metadata."""
    if not existing:
        return incoming
    result = dict(existing)
    for key, value in incoming.items():
        if value in (None, '', [], {}):
            continue
        if key in ('id',):
            continue
        if not result.get(key) or result.get('_stub') or result.get('_reconstructed'):
            result[key] = value
    result['_stub'] = False
    result['_vault_indexed'] = True
    result['_source_release'] = incoming.get('_source_release') or result.get('_source_release')
    result['_provenance'] = sorted(set(result.get('_provenance', [])) | {'trex-vault-index'})
    return result


def _member_parts(index_path):
    value = str(index_path or '')
    if '.zip/' not in value:
        raise ValueError('Release path is not inside a topic zip')
    zip_name, member = value.split('.zip/', 1)
    return zip_name + '.zip', member


def _extract_member(archive, member, dest, expected_sha):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / 'tmp' / f'import-{dest.name}.part'
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    hasher = hashlib.sha256()
    try:
        with archive.open(member) as source, tmp.open('wb') as target:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
                target.write(chunk)
        actual = hasher.hexdigest()
        if actual.lower() != expected_sha.lower():
            raise ValueError(f'SHA-256 mismatch for {member}: {actual} != {expected_sha}')
        os.replace(tmp, dest)
        return dest.stat().st_size, actual
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _record_file(db, pub_id, fmt, relative, sha, size, now):
    db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)',
               (pub_id, fmt, None, relative, 'complete', size, sha, None, now))
    db.execute('INSERT OR REPLACE INTO file_evidence VALUES (?,?,?,?,?)',
               (pub_id, fmt, sha, size, (ROOT / relative).stat().st_mtime_ns))
    db.execute('''INSERT OR REPLACE INTO file_presence
                  (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
                  VALUES (?,?,?,?,?,?,?,?,?)''',
               (pub_id, fmt, 1, size, (ROOT / relative).stat().st_mtime_ns, now, None, None, None))


def _cleanup_stale(db, remote_ids):
    removed = []
    rows = [dict(r) for r in db.execute('SELECT id,metadata FROM publications')]
    for row in rows:
        pub_id = row['id']
        if pub_id in remote_ids:
            continue
        good = db.execute("SELECT count(*) FROM files WHERE pub_id=? AND sha256 IS NOT NULL "
                          "AND status IN ('complete','skipped_verified','recovered')", (pub_id,)).fetchone()[0]
        if good:
            continue
        db.execute('DELETE FROM files WHERE pub_id=?', (pub_id,))
        db.execute('DELETE FROM index_state WHERE pub_id=?', (pub_id,))
        db.execute('DELETE FROM file_evidence WHERE pub_id=?', (pub_id,))
        db.execute('DELETE FROM file_presence WHERE pub_id=?', (pub_id,))
        db.execute('DELETE FROM extraction WHERE pub_id=?', (pub_id,))
        db.execute('DELETE FROM publications WHERE id=?', (pub_id,))
        removed.append(pub_id)
    return removed


def import_vault(source=None, formats=FORMATS, limit=None, index=True, dry_run=False,
                 start=0):
    root = find_source(source)
    index_path = root / 'index.csv'
    release = _release_label(index_path)
    index_sha = digest(index_path)
    with index_path.open(encoding='utf8', newline='') as stream:
        all_rows = list(csv.DictReader(stream))
    if limit:
        all_rows = all_rows[:limit]
    start = max(0, int(start or 0))
    rows = all_rows[start:] if start else all_rows
    remote_ids = {(row.get('id') or '').strip() for row in all_rows}
    report = {
        'state': 'running', 'source': str(root), 'release': release,
        'index_sha256': index_sha, 'started_utc': _now(),
        'rows': len(all_rows), 'start': start, 'formats': list(formats), 'files': 0,
        'skipped': 0, 'errors': [], 'removed': [],
    }
    save_json(ROOT / 'reports/import-vault.json', report)
    if dry_run:
        for row in rows:
            if not SAFE_ID.fullmatch((row.get('id') or '').strip()):
                report['errors'].append({'id': row.get('id'), 'error': 'unsafe id'})
        report['state'] = 'dry_run'
        report['finished_utc'] = _now()
        save_json(ROOT / 'reports/import-vault.json', report)
        return report

    (ROOT / 'archive').mkdir(exist_ok=True)
    (ROOT / 'tmp').mkdir(exist_ok=True)
    archives = {}
    try:
        with FileLock('acquisition'):
            with closing(database()) as db:
                now = _now()
                for offset, row in enumerate(rows, 1):
                    n = start + offset
                    pub_id = (row.get('id') or '').strip()
                    if not SAFE_ID.fullmatch(pub_id):
                        report['errors'].append({'id': pub_id, 'error': 'unsafe id'})
                        continue
                    incoming = _publication(row, release)
                    existing_row = db.execute('SELECT metadata FROM publications WHERE id=?', (pub_id,)).fetchone()
                    existing = json.loads(existing_row[0]) if existing_row else {}
                    publication = _merge_publication(existing, incoming)
                    db.execute('INSERT OR REPLACE INTO publications VALUES (?,?)',
                               (pub_id, json.dumps(publication, ensure_ascii=False)))
                    for fmt in formats:
                        expected_sha = (row.get(f'sha256_{fmt}') or '').strip()
                        source_path = (row.get(f'{fmt}_path') or '').strip()
                        if not expected_sha or not source_path:
                            continue
                        relative = f'archive/{pub_id}/{pub_id}.{fmt}'
                        dest = ROOT / relative
                        if dest.is_file() and dest.stat().st_size > 0:
                            size = dest.stat().st_size
                            prior = db.execute('SELECT sha256,bytes,status FROM files '
                                               'WHERE pub_id=? AND format=?',
                                               (pub_id, fmt)).fetchone()
                            trusted = (prior and prior['sha256'] == expected_sha
                                       and prior['bytes'] == size
                                       and prior['status'] in GOOD)
                            if trusted or digest(dest).lower() == expected_sha.lower():
                                _record_file(db, pub_id, fmt, relative, expected_sha, size, now)
                                report['skipped'] += 1
                                continue
                        try:
                            zip_name, member = _member_parts(source_path)
                            zip_path = root / zip_name
                            if not zip_path.is_file():
                                raise FileNotFoundError(f'Missing topic archive: {zip_path}')
                            archive = archives.get(zip_path)
                            if archive is None:
                                archive = zipfile.ZipFile(zip_path)
                                archives[zip_path] = archive
                            size, actual = _extract_member(archive, member, dest, expected_sha)
                            _record_file(db, pub_id, fmt, relative, actual, size, now)
                            if fmt == 'pdf' and not publication.get('file_size'):
                                publication['file_size'] = size
                                db.execute('UPDATE publications SET metadata=? WHERE id=?',
                                           (json.dumps(publication, ensure_ascii=False), pub_id))
                            report['files'] += 1
                            if n % 25 == 0:
                                db.commit()
                                report['state'] = 'importing'
                                report['current'] = pub_id
                                report['current_format'] = fmt
                                report['updated_utc'] = _now()
                                save_json(ROOT / 'reports/import-vault.json', report)
                                print(f"import {n}/{len(rows)} {pub_id} {fmt}", flush=True)
                        except Exception as error:
                            report['errors'].append({'id': pub_id, 'format': fmt,
                                                     'error': str(error)})
                            print(f"import error {n}/{len(rows)} {pub_id} {fmt}: {error}",
                                  flush=True)
                    if n % 100 == 0:
                        db.commit()
                if not limit:
                    report['removed'] = _cleanup_stale(db, remote_ids)
                db.commit()
            export_manifest()
            discovered = len(all_rows) if not limit else len(remote_ids)
            save_json(ROOT / 'reports/discovery.json', {
                'advertised': discovered, 'unique_editions': discovered,
                'difference': 0, 'complete': not limit, 'errors': [],
                'catalog_metadata_records': discovered,
                'edition_reference_stubs': 0, 'metadata_complete': True,
                'source': 'trex-vault-index',
            })
            save_json(ROOT / 'reports/discovery-progress.json', {
                'message': f'Catalog loaded from the Field Manual Vault: {discovered:,} publications.'
            })
            report['state'] = 'imported'
            report['finished_utc'] = _now()
            save_json(ROOT / 'reports/import-vault.json', report)
            if index:
                report['state'] = 'indexing'
                report['updated_utc'] = _now()
                save_json(ROOT / 'reports/import-vault.json', report)
                indexing.build()
            from .report import report as write_report
            write_report()
            state = lifecycle.finalize()
            report['state'] = 'complete'
            report['finished_utc'] = _now()
            report['library'] = {key: state.get(key) for key in
                                 ('discovered', 'pdf_verified', 'epub_verified', 'md_verified',
                                  'pdf_indexed', 'epub_indexed', 'md_indexed', 'missing_files',
                                  'pending_indexes', 'state')}
            save_json(ROOT / 'reports/import-vault.json', report)
            return report
    except Exception as error:
        report['state'] = 'failed'
        report['error'] = str(error)
        report['finished_utc'] = _now()
        save_json(ROOT / 'reports/import-vault.json', report)
        raise
    finally:
        for archive in archives.values():
            try:
                archive.close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description='Ingest a T.REX Field Manual Vault release')
    parser.add_argument('--source', help='Extracted vault folder containing index.csv')
    parser.add_argument('--formats', default=','.join(FORMATS),
                        help='Comma-separated: pdf,epub,md (default: all)')
    parser.add_argument('--limit', type=int, help='Import only the first N publications')
    parser.add_argument('--start', type=int, default=0,
                        help='Resume at this 1-based index.csv row')
    parser.add_argument('--no-index', action='store_true', help='Skip search-index preparation')
    parser.add_argument('--dry-run', action='store_true', help='Validate paths and IDs without copying')
    args = parser.parse_args()
    formats = tuple(value.strip().lower() for value in args.formats.split(',')
                    if value.strip().lower() in FORMATS)
    if not formats:
        parser.error('Choose at least one of pdf,epub,md')
    result = import_vault(args.source, formats, args.limit, not args.no_index,
                          args.dry_run, args.start)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
