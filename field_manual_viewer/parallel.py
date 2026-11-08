"""Bounded acquisition pipeline: one metadata reader, N downloaders, one indexer."""
import datetime
from contextlib import closing
import json
import os
import queue
import shutil
import threading
import time
import traceback
from .core import ROOT,database,save_json,load_json,FileLock
from .parallel_network import SharedClient,NetworkHalted


def publish_completed(completed,stop,item):
    """Apply backpressure; allow shutdown even if the indexer is busy."""
    while True:
        try:completed.put(item,timeout=.2);return True
        except queue.Full:
            if stop.is_set():return False


def should_process_edition(pub_id,good,indexed,failed,retry_failed):
    """An already acquired format can need indexing even if its sibling failed."""
    for fmt in ('pdf','epub'):
        key=(pub_id,fmt)
        if good.get(key):
            if indexed.get(key)!=good[key]:return True
        elif retry_failed or key not in failed:
            return True
    return False


def needs_acquisition(pub_id,good,failed,retry_failed):
    return any(not good.get((pub_id,fmt)) and (retry_failed or (pub_id,fmt) not in failed) for fmt in ('pdf','epub'))


def should_attempt_format(pub_id,fmt,good,failed,retry_failed):
    key=(pub_id,fmt)
    return retry_failed or key not in failed or bool(good.get(key))


def run_parallel(workers=2,limit=None,retry_failed=False,only_ids=None,test_sample=False,client=None):
    from .campaign import ensure_bulk_ready
    from .collect import acquire_file,capacity_guard,CampaignPaused,store_failure,record_outcome
    from .discover import BASE,store_detail,export_manifest
    from .source import metadata
    from .indexing import index_one,ENGINE
    from .report import report
    if workers not in (2,3,4):raise ValueError('Parallel workers must be 2, 3 or 4')
    ensure_bulk_ready(test_sample=test_sample,only_ids=only_ids,limit=limit)
    with FileLock('acquisition'),FileLock('index'):
        started=time.monotonic()
        old=load_json(ROOT/'reports/campaign.json',{})
        state={'state':'running','started_utc':old.get('started_utc') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'run_started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'pid':os.getpid(),'workers':workers,'processed_this_run':0,
               'message':f'{workers} download workers, one metadata reader and one indexer'}
        with closing(database()) as db:
            records={r[0]:json.loads(r[1]) for r in db.execute('SELECT id,metadata FROM publications ORDER BY id')}
            good={}
            for r in db.execute("SELECT pub_id,format,sha256 FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')"):
                good[(r[0],r[1])]=r[2]
            indexed={(r[0],r[1]):r[2] for r in db.execute('SELECT pub_id,format,sha256 FROM index_state WHERE error IS NULL AND engine=?',(ENGINE,))}
            failed={(r[0],r[1]) for r in db.execute("SELECT pub_id,format FROM files WHERE status IN ('failed','unavailable','detail_failed')")}
        if only_ids is not None:
            selected=set(only_ids)
            records={i:p for i,p in records.items() if i in selected}
        ids=[i for i in records if needs_acquisition(i,good,failed,retry_failed)]
        if limit:ids=ids[:limit]
        trial_index_ids=ids[:] if limit and ids else None
        state['queued_this_run']=len(ids)
        state['phase']='acquiring_missing_files'
        state['download_queue']=len(ids)
        client=client or SharedClient()
        prepared=queue.Queue(maxsize=workers*2)
        completed=queue.Queue(maxsize=workers*2)
        active={}
        active_lock=threading.Lock()
        peaks={'downloads':0}
        outcomes=[]

        def put_prepared(item):
            while not client.stop.is_set():
                try:prepared.put(item,timeout=.2);return True
                except queue.Full:pass
            return False

        def produce():
            try:
                for pub_id in ids:
                    client.check_stopped();capacity_guard()
                    try:
                        url=BASE+'/'+records[pub_id]['slug']
                        detail=metadata(client.html(url))['pub']
                        if detail['id']!=pub_id:raise ValueError('Detail redirects to a different edition')
                        p=store_detail(detail,url)
                    except NetworkHalted:raise
                    except Exception as e:
                        info={'id':pub_id,'status':'detail_failed','error':str(e)}
                        from .network import AccessDenied
                        if isinstance(e,AccessDenied):info['access_denied']=True
                        for fmt in ('pdf','epub'):store_failure(pub_id,fmt,'detail_failed',e)
                        record_outcome(info);publish_completed(completed,client.stop,(pub_id,[info]))
                        if info.get('access_denied'):client.halt(e);break
                        continue
                    if not put_prepared(p):break
            except (CampaignPaused,NetworkHalted) as e:client.halt(e)
            except BaseException as e:
                traceback.print_exc()
                client.halt('Metadata producer failed: '+str(e))
            finally:
                if not client.stop.is_set():
                    for _ in range(workers):
                        if not put_prepared(None):break

        def download():
            try:
                while not client.stop.is_set():
                    try:p=prepared.get(timeout=.2)
                    except queue.Empty:continue
                    if p is None:return
                    pub_id=p['id']
                    with active_lock:
                        active[pub_id]='downloading'
                        peaks['downloads']=max(peaks['downloads'],len(active))
                    info=[]
                    try:
                        for fmt in ('pdf','epub'):
                            if not should_attempt_format(pub_id,fmt,good,failed,retry_failed):continue
                            client.check_stopped();capacity_guard()
                            result=acquire_file(client,p,fmt)
                            info.append(result)
                            if result.get('access_denied') or result.get('halted'):
                                client.halt(result.get('error','Acquisition stopped'));break
                    except (CampaignPaused,NetworkHalted) as e:client.halt(e)
                    finally:
                        with active_lock:active.pop(pub_id,None)
                        publish_completed(completed,client.stop,(pub_id,info))
            except BaseException as e:
                traceback.print_exc()
                client.halt('Download worker failed: '+str(e))

        producer=threading.Thread(target=produce,name='field-manual-metadata',daemon=True)
        pool=[threading.Thread(target=download,name=f'field-manual-download-{i+1}',daemon=True) for i in range(workers)]

        def snapshot():
            state['updated_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            state['elapsed_seconds']=round(time.monotonic()-started,3)
            state['editions_per_minute']=round(state['processed_this_run']/max(time.monotonic()-started,.001)*60,3)
            state['free_bytes']=shutil.disk_usage(ROOT).free
            state['network']=client.metrics()
            state['ready_to_index']=completed.qsize()
            state['prepared_editions']=prepared.qsize()
            with active_lock:
                state['active_downloads']=list(active)
                state['peak_download_workers']=peaks['downloads']
            save_json(ROOT/'reports/campaign.json',state)

        snapshot()
        producer.start()
        for t in pool:t.start()
        failure=None
        last_snapshot=0
        try:
            while producer.is_alive() or any(t.is_alive() for t in pool) or not completed.empty():
                capacity_guard()
                try:pub_id,info=completed.get(timeout=.2)
                except queue.Empty:
                    if time.monotonic()-last_snapshot>2:snapshot();last_snapshot=time.monotonic()
                    continue
                outcomes.extend(info)
                state['current_id']=pub_id
                state['processed_this_run']+=1
                state['download_processed']=state['processed_this_run']
                state['message']=f'{workers} download workers: checked {state["processed_this_run"]} / {len(ids)} publications needing files'
                snapshot()
                if state['processed_this_run']%25==0:
                    export_manifest();report()
                    save_json(ROOT/'reports/acquisition-latest.json',outcomes)
            if client.stop.is_set():raise CampaignPaused(client.stop_reason or 'Shared acquisition stop requested')
            # Re-read committed file states: downloads can finish or fail during this run.
            with closing(database()) as db:
                current_good={(r[0],r[1]):r[2] for r in db.execute("SELECT pub_id,format,sha256 FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')")}
                current_indexed={(r[0],r[1]):r[2] for r in db.execute('SELECT pub_id,format,sha256 FROM index_state WHERE error IS NULL AND engine=?',(ENGINE,))}
            index_ids=[i for i in records if any(current_good.get((i,f)) and current_indexed.get((i,f))!=current_good[(i,f)] for f in ('pdf','epub'))]
            if limit:
                index_ids=index_ids[:limit] if only_ids is not None else ([i for i in index_ids if i in trial_index_ids] if trial_index_ids is not None else index_ids[:limit])
            state['phase']='indexing'
            state['index_queue']=len(index_ids)
            state['index_processed']=0
            for pub_id in index_ids:
                capacity_guard()
                state['current_id']=pub_id
                state['message']=f'Indexing {state["index_processed"] + 1} / {len(index_ids)}: {pub_id}'
                snapshot()
                errors=[r for r in index_one(pub_id) if r.get('error')]
                if errors:state['index_failures_this_run']=state.get('index_failures_this_run',0)+len(errors)
                state['index_processed']+=1
                if state['index_processed']%25==0:
                    snapshot();export_manifest();report()
        except (CampaignPaused,KeyboardInterrupt) as e:
            client.halt(str(e) or 'Interrupted; partial files retained')
            state['state']='paused';state['message']=client.stop_reason
        except Exception as e:
            client.halt(e);failure=e
            state['state']='failed';state['message']=str(e)
        finally:
            if any(t.is_alive() for t in [producer,*pool]):
                client.halt(client.stop_reason or 'Stopping pipeline')
                for t in [producer,*pool]:t.join()
            while not completed.empty():
                _,info=completed.get_nowait();outcomes.extend(info)
            if state['state']=='running':
                with closing(database()) as db:
                    total=db.execute('SELECT count(*) FROM publications').fetchone()[0]
                    good_files=db.execute("SELECT count(*) FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')").fetchone()[0]
                    index_errors=db.execute('SELECT count(*) FROM index_state WHERE error IS NOT NULL').fetchone()[0]
                state['state']='complete' if good_files==total*2 and not index_errors else ('controlled_batch_complete' if limit else 'complete_with_failures')
                scope_total=len(records) if only_ids is not None else total
                with closing(database()) as db:
                    scope_saved=sum(r[0] in records for r in db.execute("SELECT pub_id FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')")) if only_ids is not None else good_files
                state.update(count_unit='pdf_epub_files',saved_files=scope_saved,expected_files=scope_total*2,scope_publications=scope_total)
                state['message']=f'{scope_saved:,} / {scope_total*2:,} PDF + EPUB files saved for {scope_total:,} publications.'
            snapshot()
            export_manifest();report()
            save_json(ROOT/'reports/acquisition-latest.json',outcomes)
            stamp=state['run_started_utc'].replace(':','').replace('.','').replace('+','')
            save_json(ROOT/'reports'/f'parallel-run-{stamp}.json',state)
        if failure:raise failure
        return state
