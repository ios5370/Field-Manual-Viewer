"""Autonomous sequential acquisition with audited gates and durable checkpoints."""
import datetime
import hashlib
import json
import os
import shutil
import time
from .core import ROOT,database,save_json,load_json,FileLock


def fingerprint():
    h=hashlib.sha256()
    for p in sorted((ROOT/'field_manual_viewer').rglob('*')):
        if p.suffix in ('.py','.js','.css','.html'):
            h.update(str(p.relative_to(ROOT)).encode());h.update(p.read_bytes())
    return h.hexdigest()


def storage_plan(root=None, persist=True):
    """Conservative total projection minus originals/index already on this drive."""
    root=root or ROOT
    sample=load_json(root/'data/test-selection.json',[]) or load_json(root/'data/sample.json',[])
    from contextlib import closing
    with closing(database()) as db:
        saved=[dict(r) for r in db.execute("SELECT * FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')")]
        files=[r for r in saved if r['pub_id'] in sample]
        pages=sum(r[0] or 0 for r in db.execute('SELECT pages FROM extraction WHERE pub_id IN ('+','.join('?' for _ in sample)+')',sample))
        total=db.execute('SELECT count(*) FROM publications').fetchone()[0]
    archive=sum(r['bytes'] or 0 for r in files)
    per_edition=round(archive/max(len(sample),1)*total)
    per_page=round(archive/max(pages,1)*724471) if pages else 0
    projected=max(100_000_000_000,round(max(per_edition,per_page)*1.15+28_000_000_000))
    already_saved=sum(r['bytes'] or 0 for r in saved)
    already_saved+=sum(p.stat().st_size for p in (root/'data').glob('*.sqlite*') if p.is_file())
    required=max(22_000_000_000,projected-already_saved)
    free=shutil.disk_usage(root).free
    plan={'checked_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'free_bytes':free,'sample_bytes':archive,'sample_pages':pages,
        'edition_projection_bytes':per_edition,'page_projection_bytes':per_page,
        'required_free_bytes':required,'projected_total_bytes':projected,'already_saved_bytes':already_saved,
        'reserve_bytes':20_000_000_000,'sufficient':free>=required,
        'method':'At least 100 GB total planning allowance, or larger sample edition/page projection plus uncertainty and reserve, minus saved originals and database. Estimate, not a guarantee.'}
    if persist:save_json(root/'reports/storage-preflight.json',plan)
    return plan


def ensure_bulk_ready(test_sample=False, only_ids=None, limit=None):
    """Check source policy and reserve without depending on another owner's reports."""
    from .parallel_network import ensure_backoff_wait_elapsed
    ensure_backoff_wait_elapsed()
    if test_sample:
        selected=load_json(ROOT/'data/test-selection.json',[])
        if limit!=20 or not only_ids or len(only_ids)!=20 or len(set(only_ids))!=20 or set(only_ids)!=set(selected):
            raise ValueError('An early test must be restricted to its saved 20-publication selection.')
    elif not load_json(ROOT/'reports/discovery.json',{}).get('complete'):
        raise RuntimeError('Catalog discovery is incomplete; run Download-or-Resume again.')
    if shutil.disk_usage(ROOT).free < 22_000_000_000:
        raise RuntimeError('At least 22 GB free is required to start; more will be needed for the full archive.')


def run(limit=None,retry_failed=False,workers=None,only_ids=None,reconciled=False,test_sample=False,client=None):
    if not reconciled:
        from .lifecycle import reconcile_hashes
        with FileLock('setup-job'):
            reconcile_hashes()
            return run(limit,retry_failed,workers,only_ids,reconciled=True,test_sample=test_sample,client=client)
    if workers is None:workers=load_json(ROOT/'data/acquisition-settings.json',{}).get('workers',1)
    if workers not in (1,2,3,4):raise ValueError('Download workers must be between 1 and 4')
    if workers>1:
        from .parallel import run_parallel
        return run_parallel(workers,limit,retry_failed,only_ids,test_sample=test_sample,client=client)
    from .collect import collect,CampaignPaused,capacity_guard
    from .discover import export_manifest
    from .indexing import index_one
    if test_sample:raise ValueError('Early tests use the bounded parallel collector.')
    ensure_bulk_ready()
    with FileLock('acquisition'),FileLock('index'):
        old=load_json(ROOT/'reports/campaign.json',{})
        state={'state':'running','started_utc':old.get('started_utc') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'pid':os.getpid(),'processed_this_run':0,'message':'Sequential acquisition and offline indexing'}
        save_json(ROOT/'reports/campaign.json',state)
        with database() as db:
            records=[json.loads(r[0]) for r in db.execute('SELECT metadata FROM publications ORDER BY id')]
            complete={r[0] for r in db.execute("SELECT pub_id FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered') GROUP BY pub_id HAVING count(*)=2")}
            failed={r[0] for r in db.execute("SELECT DISTINCT pub_id FROM files WHERE status IN ('failed','unavailable','detail_failed')")}
        ids=[p['id'] for p in records if p['id'] not in complete and (retry_failed or p['id'] not in failed)]
        if limit:ids=ids[:limit]
        state['queued_this_run']=len(ids)

        def checkpoint(pub_id):
            state['current_id']=pub_id
            state['message']='Indexing '+pub_id
            save_json(ROOT/'reports/campaign.json',state)
            index_one(pub_id)
            state['processed_this_run']+=1
            state['updated_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            state['free_bytes']=shutil.disk_usage(ROOT).free
            state['message']=f'Processed {state["processed_this_run"]} / {len(ids)} queued publications this run'
            save_json(ROOT/'reports/campaign.json',state)
            if state['processed_this_run']%25==0:
                export_manifest()
                from .report import report
                report()

        try:
            outcomes=collect(ids,full=True,after_publication=checkpoint)
            access=[o for o in outcomes if any(t in o.get('error','') for t in ('Access denied','robots','Redirect requires'))]
            if access:raise CampaignPaused(access[-1]['error'])
            export_manifest()
            with database() as db:
                total=db.execute('SELECT count(*) FROM publications').fetchone()[0]
                done=db.execute("SELECT count(*) FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')").fetchone()[0]
            state['state']='complete' if done==total*2 else ('controlled_batch_complete' if limit else 'complete_with_failures')
            state['message']=f'{done:,} / {total*2:,} PDF + EPUB files saved for {total:,} publications; see completion report for outstanding entries'
        except (CampaignPaused,KeyboardInterrupt) as e:
            state['state']='paused';state['message']=str(e) or 'Interrupted; resume preserves partial files'
        except Exception as e:
            state['state']='failed';state['message']=str(e)
            raise
        finally:
            state['updated_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            save_json(ROOT/'reports/campaign.json',state)
            export_manifest()
            from .report import report
            report()
        return state
