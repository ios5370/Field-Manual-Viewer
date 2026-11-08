"""One-time acquisition using the existing collector and indexer."""
import argparse
import json
from contextlib import closing
from .core import ROOT, FileLock, load_json, save_json, database


def representative_ids(records):
    """A persistent, diverse pilot: branches, eras, collections and file sizes."""
    records = sorted(records, key=lambda p: p['id'])
    chosen = []
    def add(p):
        if p and p['id'] not in chosen and len(chosen) < 20:
            chosen.append(p['id'])
    for key in ('branch', 'collection'):
        for value in sorted({p[key] for p in records if p.get(key)}):
            add(next(p for p in records if p.get(key) == value))
    dated = [p for p in records if p.get('edition_year')]
    sized = [p for p in records if p.get('file_size') and p['file_size'] <= 100_000_000]
    for options, key in ((dated, 'edition_year'), (sized, 'file_size')):
        if options:
            add(min(options, key=lambda p: p[key])); add(max(options, key=lambda p: p[key]))
    for identity in ('mcdp_1', 'mcdp_1_undated'):
        add(next((p for p in records if p['id'] == identity), None))
    for p in records:
        add(p)
    return chosen


def pilot(client=None, wait_for_candidates=None):
    """Validate 20 pairs; replace up to five isolated source-file exceptions."""
    from .campaign import run
    from .indexing import ENGINE
    from .collect import CampaignPaused
    selection = load_json(ROOT / 'data/test-selection.json', [])
    if not selection:
        with closing(database()) as db:
            selection=representative_ids([json.loads(r[0]) for r in db.execute('SELECT metadata FROM publications')])
    exclusions=load_json(ROOT / 'reports/test-download.json', {}).get('source_exceptions', [])
    excluded={r['id'] for r in exclusions}
    while True:
        save_json(ROOT / 'data/test-selection.json', selection)
        result=run(limit=20, workers=4, only_ids=selection, reconciled=True,test_sample=True,client=client)
        if result['state'] in ('paused','failed'):
            raise CampaignPaused(result.get('message','Test paused'))
        failures=[];successful=set(selection)
        with closing(database()) as db:
            for identity in selection:
                for fmt in ('pdf','epub'):
                    row=db.execute("SELECT f.status,f.sha256 AS file_sha,i.sha256 AS index_sha,i.error AS index_error,i.engine,f.error FROM files f LEFT JOIN index_state i ON i.pub_id=f.pub_id AND i.format=f.format WHERE f.pub_id=? AND f.format=?",(identity,fmt)).fetchone()
                    if row and row['status'] in ('complete','skipped_verified','recovered') and row['file_sha'] and row['file_sha']==row['index_sha'] and not row['index_error'] and row['engine']==ENGINE:continue
                    successful.discard(identity)
                    source_file=bool(row and row['status'] in ('failed','unavailable') and not row['index_error'])
                    failures.append({'id':identity,'format':fmt,'error':(row['error'] if row else None) or 'Verified original or successful index missing.','source_file':source_file})
        passed=len(selection)==20 and not failures
        report={'passed':passed,'editions':selection,'failures':failures,'source_exceptions':exclusions,'campaign':result}
        save_json(ROOT / 'reports/test-download.json',report)
        if passed:return
        bad={r['id'] for r in failures}
        # Widespread failure or metadata/indexer failure still blocks acquisition.
        # A healthy 18+ pair sample demonstrates both format pipelines first.
        if len(successful)<18 or any(not r['source_file'] for r in failures) or len(excluded|bad)>5:
            raise RuntimeError('The sample test did not validate the acquisition pipeline. Review errors in Library Setup before continuing.')
        if wait_for_candidates:wait_for_candidates(set(selection)|excluded,len(bad))
        with closing(database()) as db:
            all_ids=[r[0] for r in db.execute('SELECT id FROM publications ORDER BY id')]
        replacements=[i for i in all_ids if i not in set(selection)|excluded]
        if len(replacements)<len(bad):
            raise RuntimeError('Not enough replacement publications to validate 20 pairs. See test-download.json.')
        exclusions.extend(failures);excluded.update(bad)
        report['source_exceptions']=exclusions
        save_json(ROOT / 'reports/test-download.json',report)
        selection=[i for i in selection if i not in bad]+replacements[:len(bad)]


def read_catalog_with_early_test():
    """Own both activities, sharing host pacing, backoff and cancellation."""
    import threading
    from .discover import discover,export_manifest
    from .parallel_network import SharedClient,NetworkHalted,ServerBackoff
    from .collect import capacity_guard,CampaignPaused
    from .setup_control import activity
    client=SharedClient()
    ready=threading.Event();outcome={}
    def seed(identities):
        if not load_json(ROOT/'data/test-selection.json',[]):
            save_json(ROOT/'data/test-selection.json',identities)
        ready.set()
    def read():
        try:outcome['report']=discover(on_seed=seed,client=client)
        except BaseException as error:
            if isinstance(error,(NetworkHalted,ServerBackoff)):error=CampaignPaused(str(error))
            outcome['error']=error
            client.halt(error)
        finally:ready.set()
    thread=threading.Thread(target=read,name='field-manual-catalog')
    def wait_for_candidates(excluded,count):
        while thread.is_alive():
            capacity_guard()
            if outcome.get('error'):raise outcome['error']
            with closing(database()) as db:
                if sum(r[0] not in excluded for r in db.execute('SELECT id FROM publications'))>=count:return
            thread.join(.1)
        if outcome.get('error'):raise outcome['error']
    activity('discovering','Reading catalog metadata to find the first 20 test publications.')
    thread.start()
    try:
        while not ready.wait(.1):capacity_guard()
        if outcome.get('error'):raise outcome['error']
        if len(load_json(ROOT/'data/test-selection.json',[]))!=20:
            raise RuntimeError('The source did not provide 20 test publications. Saved metadata is retained.')
        activity('acquiring','Downloading 20 test publications while reading remaining catalog metadata.')
        pilot(client=client,wait_for_candidates=wait_for_candidates)
        activity('discovering','Test batch ready. Reading remaining catalog metadata.')
        while thread.is_alive():
            capacity_guard()
            thread.join(.1)
        if outcome.get('error'):raise outcome['error']
        if not outcome.get('report',{}).get('complete'):
            raise RuntimeError('Test files are retained, but catalog metadata is unfinished. Resume later.')
    finally:
        if thread.is_alive():client.halt('Catalog reading stopped; saved progress is retained.')
        thread.join()
        export_manifest()


def execute(test=False, refresh=False, retry_failed=False, prepared=False):
    from .lifecycle import inventory, reconcile_hashes, finalize
    from .setup_control import activity
    from .collect import capacity_guard, CampaignPaused
    with FileLock('download-startup'):
        if not prepared:
            reconcile_hashes(lambda text: activity('verifying', text))
        snapshot = inventory()
        if test and snapshot['test_ready'] and not refresh:
            finalize()
            return
        if snapshot['discovered'] and not snapshot['missing_files'] and not refresh and load_json(ROOT/'reports/discovery.json',{}).get('complete'):
            if snapshot['pending_indexes']:
                from .indexing import build
                activity('indexing', 'Preparing your documents for searching.')
                build()
            finalize()
            return
        capacity_guard()
        if snapshot['free_bytes'] < 22_000_000_000:
            raise CampaignPaused('Free at least 22 GB before downloading. A full collection requires considerably more.')
        from .parallel_network import ensure_backoff_wait_elapsed
        ensure_backoff_wait_elapsed()
        if retry_failed:
            # Recovery is scoped to recorded problems, never the entire unacquired catalog.
            with closing(database()) as db:
                repair_ids = [r[0] for r in db.execute("SELECT DISTINCT pub_id FROM files WHERE status IN ('failed','unavailable','detail_failed','missing','damaged','interrupted','checking_or_downloading')")]
            if repair_ids:
                from .campaign import run
                activity('acquiring', 'Repairing recorded file exceptions.')
                result = run(workers=4, retry_failed=True, only_ids=repair_ids, reconciled=True)
                if result['state'] in ('paused', 'failed'):
                    raise CampaignPaused(result.get('message', 'Repair paused'))
            finalize()
            return
        if refresh or not load_json(ROOT / 'reports/discovery.json', {}).get('complete'):
            if not refresh and not snapshot['test_ready']:
                read_catalog_with_early_test()
                if test:
                    finalize()
                    return
            else:
                from .discover import discover
                activity('discovering', 'Reading catalog metadata.')
                result = discover(refresh=refresh)
                if not result.get('complete'):
                    raise RuntimeError('Catalog metadata is unfinished. Your files are safe; resume later.')
        activity('acquiring', 'Downloading your sample.' if test else 'Downloading your library.')
        if test or not load_json(ROOT / 'reports/test-download.json', {}).get('passed'):
            pilot()
            if test:
                finalize()
                return
        from .campaign import run
        result = run(workers=4, retry_failed=retry_failed, reconciled=True)
        if result['state'] in ('paused', 'failed'):
            raise CampaignPaused(result.get('message', 'Preparation paused'))
        finalize()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--authorize', action='store_true')
    args = parser.parse_args()
    if not args.test and not args.authorize:
        parser.error('Use Set-Up-Library.bat to confirm downloading.')
    from .setup_control import start_job
    print(json.dumps(start_job('test' if args.test else 'refresh' if args.refresh else 'download', authorized=True)))


if __name__ == '__main__':
    main()
