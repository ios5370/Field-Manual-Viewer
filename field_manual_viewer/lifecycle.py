"""Local library inventory and durable completion evidence. No network calls."""
import datetime
import json
import shutil
from contextlib import closing
from .core import ROOT, database, load_json, save_json, FileLock, digest, original_evidence_current
from .indexing import ENGINE

GOOD = ('complete', 'skipped_verified', 'recovered')
SOURCE_ERRORS = ('failed', 'unavailable', 'detail_failed')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def locked(name):
    try:
        with FileLock(name):
            return False
    except RuntimeError:
        return True


def original_path(row):
    path = (ROOT / row['path']).resolve()
    if not path.is_relative_to((ROOT / 'archive').resolve()):
        raise ValueError('Original path is outside the archive')
    return path


def inventory():
    """Read the same ledger as the collector; check local file presence and size."""
    with closing(database()) as db:
        total = db.execute('SELECT count(*) FROM publications').fetchone()[0]
        all_ids = {r[0] for r in db.execute('SELECT id FROM publications')}
        reconstructed = db.execute("SELECT count(*) FROM publications WHERE json_extract(metadata,'$._reconstructed')").fetchone()[0]
        files = [dict(r) for r in db.execute('SELECT * FROM files')]
        indexes = {(r['pub_id'], r['format']): dict(r) for r in db.execute('SELECT * FROM index_state')}
        evidence = {(r['pub_id'], r['format']): dict(r) for r in db.execute('SELECT * FROM file_evidence')}
    good, issues, source_errors = {}, [], set()
    verification_needed = 0
    pending_verification = 0
    repair_needed = 0
    for row in files:
        key = (row['pub_id'], row['format'])
        if row['status'] in SOURCE_ERRORS:
            source_errors.add(key)
        if row['status'] == 'pending_verification':
            try:
                present = original_path(row).is_file()
            except (OSError, ValueError):
                present = False
            if present:
                pending_verification += 1
                verification_needed += 1
        if row['status'] in GOOD and row['sha256']:
            try:
                path = original_path(row)
                valid = path.is_file() and path.stat().st_size == row['bytes']
                stamp = evidence.get(key, {})
                if valid and not original_evidence_current(ROOT, row, stamp):
                    verification_needed += 1
                    valid = False
            except (OSError, ValueError):
                valid = False
            if valid:
                good[key] = row
            else:
                issues.append({'id': row['pub_id'], 'format': row['format'], 'error': 'Original is missing or its size changed.'})
        elif row.get('error'):
            issues.append({'id': row['pub_id'], 'format': row['format'], 'error': row['error']})
        if row['status'] in ('damaged', 'missing'):
            repair_needed += 1
    indexed = {}
    for key, row in good.items():
        index = indexes.get(key, {})
        if not index.get('error') and index.get('engine') == ENGINE and index.get('sha256') == row['sha256']:
            indexed[key] = index
        elif index.get('error'):
            issues.append({'id': key[0], 'format': key[1], 'error': 'Index: ' + index['error']})
    editions = {key[0] for key in good}
    pairs = {key[0] for key in good if (key[0], 'pdf') in good and (key[0], 'epub') in good}
    discovery = load_json(ROOT / 'reports/discovery.json', {})
    recovered_catalog = bool(total and reconstructed == total)
    catalog_complete = bool(discovery.get('complete')) or recovered_catalog
    # A reconstructed library only knows the formats that actually survive.
    # Do not invent missing EPUBs (or PDFs) that were never present.
    expected_files = len(files) if recovered_catalog else total * 2
    missing = max(0, expected_files - len(good))
    outstanding = len(all_ids - editions) if recovered_catalog else total - len(pairs)
    pending = len(good) - len(indexed)
    test_selection = load_json(ROOT / 'data/test-selection.json', [])
    test_ready = len(test_selection) == 20 and all((identity,fmt) in good and (identity,fmt) in indexed
                  for identity in test_selection for fmt in ('pdf','epub'))
    active = locked('setup-job') or locked('acquisition') or locked('index')
    job = load_json(ROOT / 'reports/setup-job.json', {})
    campaign = load_json(ROOT / 'reports/campaign.json', {})
    if active:
        phase = job.get('phase', 'working')
        message = job.get('message', 'Preparing your library.')
        if phase == 'acquiring':
            phase = campaign.get('phase', phase)
            message = campaign.get('message', message)
        elif phase in ('discovering', 'indexing'):
            report = 'discovery-progress.json' if phase == 'discovering' else 'indexing-progress.json'
            message = load_json(ROOT / 'reports' / report, {}).get('message', message)
            if phase=='discovering' and test_ready:message='Test batch ready. '+message
        state = 'working'
    elif total == 0:
        state, phase, message = 'new', 'new', 'Your library is ready to download.'
    elif verification_needed:
        state, phase, message = 'verify_pending', 'verifying', 'Check your saved originals before continuing. This works offline.'
    elif not catalog_complete:
        state, phase, message = 'paused', 'paused', 'Catalog metadata is unfinished. Your saved files and catalog progress are safe.'
    elif repair_needed:
        state, phase, message = 'repair_pending', 'repair', 'Some saved originals need repair. Your other files are safe.'
    elif missing == 0 and pending == 0:
        state, phase, message = 'complete', 'complete', 'Your offline library is ready.'
    elif missing == 0:
        state, phase, message = 'index_pending', 'indexing', 'Preparing your documents for searching.'
    elif missing <= len(source_errors) and pending == 0:
        state, phase, message = 'exceptions', 'exceptions', 'Your offline library is ready with documented exceptions.'
    else:
        state, phase, message = 'paused', 'paused', 'Your previously downloaded files are safe.'
        if load_json(ROOT / 'reports/test-download.json', {}).get('passed'):
            message = 'The sample is ready. Download the rest of your library when you are ready.'
    from .campaign import storage_plan
    plan=storage_plan(ROOT,persist=False)
    catalog_message = load_json(ROOT/'reports/discovery-progress.json',{}).get('message','Reading catalog metadata.')
    if recovered_catalog:
        catalog_message = 'Catalog recovered from archived documents.'
    return {'catalog_complete':catalog_complete,'catalog_expected':discovery.get('advertised',total),
            'catalog_message':catalog_message,'recovered_catalog':recovered_catalog,
            'test_mode':job.get('action')=='test',
            'test_files_verified':sum((identity,fmt) in good for identity in test_selection for fmt in ('pdf','epub')),
            'test_files_indexed':sum((identity,fmt) in indexed for identity in test_selection for fmt in ('pdf','epub')),
            'recommended_free_bytes':plan['required_free_bytes'], 'storage_sufficient':plan['sufficient'], 'state': state, 'phase': phase, 'message': message, 'active': active,
            'discovered': total, 'available': len(editions), 'complete_pairs': len(pairs),
            'pdf_verified': sum(k[1] == 'pdf' for k in good),
            'epub_verified': sum(k[1] == 'epub' for k in good),
            'pdf_indexed': sum(k[1] == 'pdf' for k in indexed),
            'epub_indexed': sum(k[1] == 'epub' for k in indexed),
            'missing_files': missing, 'outstanding_publications': outstanding,
            'expected_files': expected_files,
            'pending_indexes': pending, 'source_exceptions': len(source_errors),
            'verification_needed': verification_needed, 'pending_verification': pending_verification,
            'test_ready': test_ready, 'test_started': bool(test_selection),
            'free_bytes': shutil.disk_usage(ROOT).free, 'issues': issues,
            'last_error': job.get('error', ''), 'updated_utc': now()}


def reconcile_hashes(progress=None, deep=True):
    """Before acquisition, preserve SHA records and check existing originals locally."""
    with closing(database()) as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM files WHERE sha256 IS NOT NULL')]
        evidence = {(r['pub_id'], r['format']): dict(r) for r in db.execute('SELECT * FROM file_evidence')}
    issues = []
    for n, row in enumerate(rows, 1):
        from .collect import CampaignPaused
        if (ROOT / 'data/pause-acquisition').exists():
            raise CampaignPaused('Paused; saved originals are retained.')
        if progress:
            progress(f'Checking saved originals: {n} / {len(rows)}')
        try:
            path = original_path(row)
            if not deep and original_evidence_current(ROOT, row, evidence.get((row['pub_id'], row['format']), {})):
                continue
            status = 'missing' if not path.is_file() else ('recovered' if path.stat().st_size == row['bytes'] and digest(path) == row['sha256'] else 'damaged')
        except (OSError, ValueError):
            status = 'missing'
        error = None if status == 'recovered' else 'Original is missing or does not match its saved SHA-256.'
        if error:
            issues.append({'id': row['pub_id'], 'format': row['format'], 'error': error, 'sha256': row['sha256']})
        with closing(database()) as db, db:
            db.execute('UPDATE files SET status=?,error=?,updated=? WHERE pub_id=? AND format=?',
                       (status, error, now(), row['pub_id'], row['format']))
            if status == 'recovered':
                db.execute('INSERT OR REPLACE INTO file_evidence VALUES (?,?,?,?,?)',
                           (row['pub_id'], row['format'], row['sha256'], row['bytes'], path.stat().st_mtime_ns))
            else:
                db.execute('DELETE FROM file_evidence WHERE pub_id=? AND format=?', (row['pub_id'], row['format']))
    save_json(ROOT / 'reports/integrity-check.json', {'checked_utc': now(), 'checked': len(rows), 'issues': issues, 'passed': not issues})
    return issues


def finalize():
    state = inventory()
    ready = bool(load_json(ROOT / 'reports/discovery.json', {}).get('complete')) and state['discovered'] > 0 and state['pending_indexes'] == 0 and state['missing_files'] <= state['source_exceptions']
    save_json(ROOT / 'data/completion.json', {'ready': ready, 'recorded_utc': now(),
              'discovered': state['discovered'], 'verified_files': state['pdf_verified'] + state['epub_verified'],
              'indexed_files': state['pdf_indexed'] + state['epub_indexed'],
              'missing_files': state['missing_files'], 'exceptions': state['issues']})
    return state
