"""Sequential, durable, range-aware archive acquisition."""
from contextlib import closing
import datetime
import json
import re
import time
import urllib.error
import zipfile
import shutil
import threading
from pathlib import Path
from pypdf import PdfReader
from .core import ROOT, database, digest, save_json, load_json
from .discover import BASE, merge, persist, store_detail, export_manifest
from .network import Client, AccessDenied
from .source import metadata


class CampaignPaused(RuntimeError):
    pass


EPUB_MAX_ENTRY_BYTES = 512 * 1024 * 1024
EPUB_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
EPUB_MAX_RATIO = 800
EPUB_MAX_ENTRIES = 10000


def validate_epub_container(archive):
    """Reject decompression bombs before testzip or text extraction reads them."""
    infos = archive.infolist()
    if len(infos) > EPUB_MAX_ENTRIES:
        raise ValueError('EPUB has too many entries to process safely')
    total = 0
    for info in infos:
        total += info.file_size
        if info.file_size > EPUB_MAX_ENTRY_BYTES:
            raise ValueError('EPUB entry is too large to process safely')
        if info.compress_size and info.file_size > info.compress_size * EPUB_MAX_RATIO:
            raise ValueError('EPUB compression ratio is unsafe')
    if total > EPUB_MAX_TOTAL_BYTES:
        raise ValueError('EPUB uncompressed size is unsafe')
    return infos


def capacity_guard(remaining=0):
    if (ROOT/'data/pause-acquisition').exists():
        raise CampaignPaused('Paused by user; partial files retained')
    if shutil.disk_usage(ROOT).free < 20_000_000_000 + max(0,remaining):
        raise CampaignPaused('Disk reserve reached; free space and resume')


def validate(path, fmt, size=None):
    path = Path(path)
    if size is not None and path.stat().st_size != size:
        raise ValueError(f'Size mismatch: {path.stat().st_size} != {size}')
    with path.open('rb') as f:
        sig = f.read(8)
    if fmt == 'pdf':
        if not sig.startswith(b'%PDF-'):
            raise ValueError('PDF signature mismatch')
        with path.open('rb') as f:
            f.seek(max(0, path.stat().st_size - 2048))
            if b'%%EOF' not in f.read():
                raise ValueError('PDF missing end marker')
        if not len(PdfReader(path).pages):
            raise ValueError('PDF has no readable pages')
    elif fmt == 'epub':
        if not sig.startswith(b'PK'):
            raise ValueError('EPUB ZIP signature mismatch')
        with zipfile.ZipFile(path) as z:
            validate_epub_container(z)
            if z.read('mimetype') != b'application/epub+zip' or z.testzip():
                raise ValueError('Invalid EPUB mimetype or ZIP CRC')
            if 'META-INF/container.xml' not in z.namelist():
                raise ValueError('EPUB container missing')
    else:
        raise ValueError('Unsupported archive format')
    return digest(path)


def transfer(client, url, dest, fmt, expected=None, interrupt_after=None):
    """Resume only against a persisted HTTP validator; otherwise safely restart."""
    dest = Path(dest)
    check_stopped=getattr(client,'check_stopped',lambda:None)
    check_stopped()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + '.part')
    state_path = part.with_suffix(part.suffix + '.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    offset = part.stat().st_size if part.exists() else 0
    known_total = state.get('total')
    if offset and known_total is not None and offset == known_total:
        try:
            sha = validate(part, fmt, known_total)
            part.replace(dest)
            state_path.unlink(missing_ok=True)
            return {'bytes': known_total, 'sha256': sha, 'resumed_from': offset,
                    'catalog_bytes': expected, 'catalog_size_disagreement': expected is not None and known_total != expected,
                    'content_type': 'recovered-complete-part'}
        except Exception:
            part.replace(part.with_suffix(part.suffix + '.rejected'))
            state_path.unlink(missing_ok=True)
            state, offset = {}, 0
    etag=state.get('etag')
    validator = (etag if etag and not etag.startswith('W/') else None) or state.get('last_modified')
    headers = {}
    if offset and validator and state.get('url') == url:
        headers = {'Range': f'bytes={offset}-', 'If-Range': validator}
    else:
        offset = 0
    with client.open(url, headers) as r:
        content_type = r.headers.get('Content-Type', '').split(';')[0].lower()
        allowed = {'pdf': ('application/pdf', 'application/octet-stream'),
                   'epub': ('application/epub+zip', 'application/zip', 'application/octet-stream')}
        if content_type not in allowed[fmt]:
            raise ValueError(f'Unexpected content type: {content_type}')
        if r.status == 206:
            m = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', r.headers.get('Content-Range', ''))
            if not headers or not m or int(m[1]) != offset:
                raise ValueError('Invalid Content-Range')
            total = int(m[3])
        elif r.status == 200:
            offset = 0
            total = int(r.headers['Content-Length']) if r.headers.get('Content-Length') else None
        else:
            raise ValueError(f'Unexpected response: {r.status}')
        save_json(state_path, {'url': url, 'etag': r.headers.get('ETag'),
                              'last_modified': r.headers.get('Last-Modified'), 'total': total})
        with part.open('ab' if offset else 'wb') as f:
            written = 0
            capacity_guard((total or 0)-offset)
            while chunk := r.read(128 * 1024):
                check_stopped()
                f.write(chunk)
                written += len(chunk)
                if written % (2*1024*1024)==0:
                    capacity_guard()
                if interrupt_after and written >= interrupt_after:
                    f.flush()
                    raise InterruptedError('Deliberate validation interruption')
    sha = validate(part, fmt, total)
    part.replace(dest)
    state_path.unlink(missing_ok=True)
    return {'bytes': dest.stat().st_size, 'sha256': sha, 'resumed_from': offset,
            'content_type': content_type, 'catalog_bytes': expected,
            'catalog_size_disagreement': expected is not None and dest.stat().st_size != expected}


def select_sample():
    records = {p['id']: p for p in json.loads((ROOT / 'data/manifest.json').read_text(encoding='utf8'))}
    featured = metadata((ROOT / 'data/discovery/catalog.html').read_text(encoding='utf8'))['landing']['featured']['pubs']
    # Include the two Warfighting editions, an undated record, large PDFs,
    # historical material, and all five service branches when discovered.
    selected = [p['id'] for p in featured[:16]]
    selected += ['mcdp_1', 'fm_31-21_1969-01']
    for branch in ('navy', 'joint'):
        options = [p for p in records.values() if p.get('branch') == branch and p.get('file_size')]
        if options:
            selected.append(min(options, key=lambda p: p['file_size'])['id'])
    for p in featured:
        if len(selected) >= 20:
            break
        if p['id'] not in selected:
            selected.append(p['id'])
    save_json(ROOT / 'data/sample.json', selected)
    return selected


_AUDIT_LOCK = threading.Lock()


def record_outcome(info):
    with _AUDIT_LOCK:
        with (ROOT/'reports/acquisition-history.jsonl').open('a',encoding='utf8') as log:
            log.write(json.dumps(info)+'\n')
        print(info['id'],info.get('format','detail'),info['status'],info.get('bytes',''),info.get('error',''),flush=True)


def store_failure(pub_id,fmt,status,error):
    with closing(database()) as db, db:
        db.execute("""INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(pub_id,format) DO UPDATE SET status=excluded.status,error=excluded.error,
            updated=excluded.updated WHERE files.sha256 IS NULL""",
            (pub_id,fmt,None,f'archive/{pub_id}/{pub_id}.{fmt}',status,None,None,str(error),
             datetime.datetime.now(datetime.timezone.utc).isoformat()))


def acquire_file(client,p,fmt,interrupt_id=None):
    """One edition/format owns its temporary file and SQLite record exclusively."""
    from .parallel_network import NetworkHalted
    pub_id=p['id']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',pub_id):raise ValueError('Unsafe edition ID')
    relative=f'archive/{pub_id}/{pub_id}.{fmt}'
    info={'id':pub_id,'format':fmt,'path':relative}
    file_url=p.get(fmt+'_url')
    if not file_url:
        info.update(status='unavailable',error='No public asset link')
        store_failure(pub_id,fmt,'unavailable',info['error'])
        record_outcome(info)
        return info
    dest=ROOT/relative
    expected=next((a.get('file_size') for a in p.get('assets',[]) if a['format']==fmt),None)
    with closing(database()) as db, db:
        prior=db.execute('SELECT * FROM files WHERE pub_id=? AND format=?',(pub_id,fmt)).fetchone()
        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)',
            (pub_id,fmt,file_url,relative,'checking_or_downloading',prior['bytes'] if prior else None,
             prior['sha256'] if prior else None,None,datetime.datetime.now(datetime.timezone.utc).isoformat()))
    try:
        if dest.exists() and prior and prior['sha256']:
            # Matching the stored SHA-256 and size proves this is the exact file
            # that already passed format validation, without reparsing it.
            try:sha=digest(dest) if dest.stat().st_size==prior['bytes'] else None
            except Exception:sha=None
            if sha and sha==prior['sha256']:
                info.update(status='skipped_verified',bytes=dest.stat().st_size,sha256=sha)
            else:
                dest.replace(dest.with_suffix(dest.suffix+'.corrupt-'+str(time.time_ns())))
        elif dest.exists():
            sha=validate(dest,fmt)
            info.update(status='recovered',bytes=dest.stat().st_size,sha256=sha)
        if 'status' not in info:
            for attempt in range(3):
                try:
                    capacity_guard()
                    result=transfer(client,file_url,dest,fmt,expected,
                        256*1024 if interrupt_id==pub_id else None)
                    info.update(result,status='complete')
                    break
                except (AccessDenied,InterruptedError,CampaignPaused,NetworkHalted):raise
                except Exception:
                    if attempt==2:raise
                    part=dest.with_suffix(dest.suffix+'.part')
                    state_path=part.with_suffix(part.suffix+'.json')
                    total=load_json(state_path,{}).get('total')
                    if part.exists() and total is not None and part.stat().st_size>=total:
                        part.replace(part.with_suffix(part.suffix+'.rejected-'+str(time.time_ns())))
                        state_path.unlink(missing_ok=True)
                    # Interruptible backoff when the parallel pool has stopped.
                    stop=getattr(client,'stop',None)
                    if stop:
                        if stop.wait(3*2**attempt):raise NetworkHalted(client.stop_reason or 'Shared acquisition stop requested')
                    else:time.sleep(3*2**attempt)
    except Exception as e:
        halted=isinstance(e,(CampaignPaused,NetworkHalted))
        info.update(status='interrupted' if halted or isinstance(e,InterruptedError) else 'failed',error=str(e))
        if halted:info['halted']=True
        if isinstance(e,AccessDenied):info['access_denied']=True
    with closing(database()) as db, db:
        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)',
            (pub_id,fmt,file_url,relative,info['status'],info.get('bytes'),info.get('sha256'),
             info.get('error'),datetime.datetime.now(datetime.timezone.utc).isoformat()))
    if info.get('sha256') and info['status'] in ('complete','skipped_verified','recovered'):
        with closing(database()) as db, db:
            db.execute('INSERT OR REPLACE INTO file_evidence VALUES (?,?,?,?,?)',(pub_id,fmt,info['sha256'],info['bytes'],dest.stat().st_mtime_ns))
    if info.get('bytes') is not None:
        info['catalog_bytes']=expected
        info['catalog_size_disagreement']=expected is not None and info['bytes']!=expected
    record_outcome(info)
    return info


def collect(ids=None,formats=('pdf','epub'),interrupt_id=None,full=False,after_publication=None):
    with closing(database()) as db, db:
        records={r[0]:json.loads(r[1]) for r in db.execute('SELECT id,metadata FROM publications')}
    approved=load_json(ROOT/'data/sample.json',[])
    ids=approved if ids is None else list(ids)
    if full:
        from .campaign import ensure_bulk_ready
        ensure_bulk_ready()
    elif len(ids)>25 or not set(ids)<=set(approved):
        raise ValueError('Sample collector only acquires the fixed approved sample; use qualified bulk mode')
    client,outcomes=Client(),[]
    for pub_id in ids:
        capacity_guard()
        try:
            url=BASE+'/'+records[pub_id]['slug']
            detail=metadata(client.html(url))['pub']
            if detail['id']!=pub_id:raise ValueError('Detail redirects to a different edition')
            p=store_detail(detail,url)
        except Exception as e:
            info={'id':pub_id,'status':'detail_failed','error':str(e)}
            if isinstance(e,AccessDenied):info['access_denied']=True
            outcomes.append(info);record_outcome(info)
            for fmt in formats:store_failure(pub_id,fmt,'detail_failed',e)
            if isinstance(e,AccessDenied):break
            continue
        for fmt in formats:
            info=acquire_file(client,p,fmt,interrupt_id)
            outcomes.append(info)
            save_json(ROOT/'reports/acquisition-latest.json',outcomes)
            if info.get('halted'):raise CampaignPaused(info['error'])
            if info.get('access_denied') or info['status']=='interrupted':return outcomes
        if after_publication:after_publication(pub_id)
    export_manifest()
    save_json(ROOT/'reports/acquisition-latest.json',outcomes)
    return outcomes
