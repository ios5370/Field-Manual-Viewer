"""Offline catalog queries with publication-level paging and FTS excerpts."""
import collections
import json
import math
import re
import sqlite3
import threading
import time
from contextlib import closing
from .indexing import ENGINE
from . import core
from .core import ROOT, database, load_json, original_evidence_current

_CACHE_LOCK = threading.RLock()
_CACHE_BUILD_LOCK = threading.Lock()
_CACHE = None


GOOD = ('complete', 'skipped_verified', 'recovered')


def original_available(row, evidence, presence=None, cached=True):
    """Cached availability for browsing; the reader boundary still checks the file."""
    if row['status'] not in GOOD or not row['sha256']:
        return False
    key = (row['pub_id'], row['format'])
    stamp = evidence.get(key, {})
    if cached and presence is not None and key in presence:
        record = presence[key]
        return bool(record.get('present')
                    and record.get('bytes') == row['bytes']
                    and stamp.get('sha256') == row['sha256']
                    and stamp.get('bytes') == row['bytes']
                    and stamp.get('mtime_ns') is not None
                    and record.get('mtime_ns') == stamp.get('mtime_ns'))
    return original_evidence_current(ROOT, row, stamp)


def file_evidence(db):
    return {(r['pub_id'],r['format']):dict(r) for r in db.execute('SELECT * FROM file_evidence')}


def file_presence(db):
    return {(r['pub_id'],r['format']):dict(r) for r in db.execute('SELECT * FROM file_presence')}


def invalidate_cache():
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def _cache_key(db):
    revision = db.execute("SELECT value FROM reconcile_state WHERE key='revision'").fetchone()
    files = db.execute("SELECT count(*),coalesce(sum(bytes),0),coalesce(max(updated),''),"
                       "coalesce(sum(length(updated)),0),coalesce(group_concat(status,''),'') "
                       "FROM files").fetchone()
    publications = db.execute('SELECT count(*),coalesce(sum(length(metadata)),0) FROM publications').fetchone()
    indexes = db.execute("SELECT count(*),coalesce(sum(segments),0),coalesce(sum(length(error)),0) "
                         "FROM index_state").fetchone()
    return (str((core.ROOT/'data/field-manual-viewer.sqlite').resolve()),
            int(revision[0]) if revision and str(revision[0]).isdigit() else 0,
            tuple(files), tuple(publications), tuple(indexes))


def _catalog_data(db):
    """Reuse parsed catalog rows until the database's visible state changes."""
    global _CACHE
    key = _cache_key(db)
    with _CACHE_LOCK:
        if _CACHE and _CACHE['key'] == key:
            return _CACHE
    with _CACHE_BUILD_LOCK:
        # Another request may have populated the cache while we waited.
        key = _cache_key(db)
        with _CACHE_LOCK:
            if _CACHE and _CACHE['key'] == key:
                return _CACHE
        data = {
            'key': key,
            'publications': {r['id']: json.loads(r['metadata'])
                             for r in db.execute('SELECT id,metadata FROM publications')},
            'files': [dict(r) for r in db.execute('SELECT * FROM files')],
            'evidence': file_evidence(db),
            'presence': file_presence(db),
            'indexes': [dict(r) for r in db.execute('SELECT * FROM index_state WHERE error IS NULL')],
        }
        with _CACHE_LOCK:
            _CACHE = data
        return data


def prewarm():
    """Parse the catalog in the background before the browser asks for it."""
    try:
        with closing(database()) as db:
            _catalog_data(db)
    except Exception:
        pass


def expression(query):
    return ' AND '.join('"' + (a or b).replace('"', '""') + '"'
                        for a, b in re.findall(r'"([^"]+)"|(\S+)', query))


def status():
    with closing(database()) as db:
        data = _catalog_data(db)
    evidence, files, indexes = data['evidence'], data['files'], data['indexes']
    presence = data['presence']
    total = len(data['publications'])
    good={(f['pub_id'],f['format']):f for f in files if original_available(f,evidence,presence)}
    counts={fmt:{'format':fmt,'files':sum(key[1]==fmt for key in good),
                 'bytes':sum(f['bytes'] for key,f in good.items() if key[1]==fmt)} for fmt in ('pdf','epub')}
    indexed=dict(collections.Counter(i['format'] for i in indexes if i['engine']==ENGINE and (i['pub_id'],i['format']) in good
                  and i['sha256']==good[(i['pub_id'],i['format'])]['sha256']))
    acquired=len({key[0] for key in good})
    errors=sum(f['status'] in ('failed','unavailable','detail_failed','missing','damaged') or
               (f['status'] in ('complete','skipped_verified','recovered') and (f['pub_id'],f['format']) not in good) for f in files)
    return {'total': total, 'acquired': acquired, 'formats': counts, 'indexed': indexed,
            'failed_files': errors, 'complete': total>0 and all(counts.get(f, {}).get('files', 0) == total for f in ('pdf','epub')),
            'pending_verification': sum(1 for f in files if f['status'] == 'pending_verification'),
            'missing_presence': sum(1 for key, row in presence.items() if not row.get('present')),
            'reconcile': load_json(ROOT / 'reports/reconcile.json', {}),
            'campaign': load_json(ROOT / 'reports/campaign.json', {'state': 'not_started'}),
            'storage': load_json(ROOT / 'reports/storage-preflight.json', {})}


def details(pub_id):
    db = database()
    evidence = file_evidence(db)
    row = db.execute('SELECT metadata FROM publications WHERE id=?', (pub_id,)).fetchone()
    if not row:
        db.close(); return None
    p = json.loads(row[0])
    p['reconstructed'] = bool(p.get('_reconstructed'))
    presence = {(r['pub_id'], r['format']): dict(r) for r in db.execute('SELECT * FROM file_presence WHERE pub_id=?', (pub_id,))}
    p['files'] = [dict(f) for f in db.execute('SELECT * FROM files WHERE pub_id=?', (pub_id,))]
    for file in p['files']:
        file['available'] = original_available(file,evidence,presence)
        record = presence.get((file['pub_id'], file['format']))
        file['present'] = bool(record.get('present')) if record is not None else file['available']
    p['index'] = [dict(f) for f in db.execute('SELECT * FROM index_state WHERE pub_id=?', (pub_id,))]
    editions = []
    for e in p.get('editions', []):
        r = db.execute('SELECT metadata FROM publications WHERE id=?', (e['id'],)).fetchone()
        local = json.loads(r[0]) if r else e
        editions.append({**e, 'title': local.get('title'), 'local': bool(r)})
    p['related_editions'] = editions
    db.close()
    return p


def browse(params, timeout_seconds=20):
    with closing(database()) as db:
        deadline=time.monotonic()+timeout_seconds
        db.set_progress_handler(lambda: time.monotonic()>deadline,1000)
        try:
            data=_catalog_data(db)
            return _browse(db,params,data)
        except sqlite3.OperationalError as error:
            if str(error)=='interrupted':
                raise ValueError('This search exceeded 20 seconds. Try a more specific phrase or add another word.') from error
            raise


def _browse(db,params,data=None):
    if data is None:
        data=_catalog_data(db)
    evidence, presence = data['evidence'], data['presence']
    publications = data['publications']
    available = collections.defaultdict(dict)
    for r in data['files']:
        if r['sha256'] and r['status'] in ('complete','skipped_verified','recovered'):
            if original_available(r,evidence,presence): available[r['pub_id']][r['format']] = dict(r)
    indexed = collections.defaultdict(set)
    segments = {}
    files_by_key = {(f['pub_id'],f['format']): f for f in data['files']}
    for index in data['indexes']:
        source = files_by_key.get((index['pub_id'], index['format']))
        if not source or index['engine'] != ENGINE or index['sha256'] != source['sha256']:
            continue
        indexed[index['pub_id']].add(index['format'])
        segments[(index['pub_id'],index['format'])] = max(1,index['segments'] or 0)
    facets = {key: sorted({str(p[key]) for p in publications.values() if p.get(key)})
              for key in ('branch','pub_type','functional_category','collection')}
    ids = set(publications)
    for key in ('branch','pub_type','functional_category','collection'):
        if params.get(key):
            ids = {i for i in ids if publications[i].get(key) == params[key] or
                   (key == 'branch' and params[key] in publications[i].get('services', []))}
    for key, op in (('year_from', lambda a,b:a >= b), ('year_to', lambda a,b:a <= b)):
        if params.get(key):
            year = int(params[key])
            ids = {i for i in ids if publications[i].get('edition_year') and op(publications[i]['edition_year'], year)}
    if params.get('availability', 'archived') == 'archived':
        ids &= set(available)
    query, scope = params.get('q', '').strip(), params.get('scope', 'all')
    fmt = params.get('format', 'both')
    hits, scores, catalog_matches = collections.defaultdict(list), {}, set()
    content_matches=set()
    if query:
        expr = expression(query)
        terms = [a or b for a,b in re.findall(r'"([^"]+)"|(\S+)', query.lower())]
        if scope in ('all','metadata','title','designation'):
            for i in ids:
                p = publications[i]
                fields = [p.get(scope, '')] if scope in ('title','designation') else [p.get(k) or '' for k in
                    ('title','designation','id','purpose','edition_date','branch','pub_type','functional_category','collection')]
                text = ' '.join(map(str, fields)).lower()
                if all(t in text for t in terms):
                    catalog_matches.add(i)
        if ids and scope in ('all','body','toc'):
            # Group cheap match counts first. Rank passages only for the visible
            # publications, rather than scoring/sorting every archived page.
            for table,kind in (('passages','pdf'),('epub_passages','epub')):
                if fmt not in ('both',kind) or (kind=='epub' and scope=='toc'):continue
                match_expr=expr if scope=='all' or kind=='epub' else scope+' : ('+expr+')'
                for r in db.execute(f'SELECT pub_id,count(*) AS matches FROM {table} WHERE {table} MATCH ? GROUP BY pub_id',(match_expr,)):
                    identity=r['pub_id']
                    if identity not in ids or kind not in available[identity] or kind not in indexed[identity]:continue
                    # Search hits must not outlive the actual original just because
                    # the catalog cache still lists it.  Check only matched formats.
                    if not original_available(available[identity][kind],evidence,presence,cached=False):continue
                    content_matches.add(identity)
                    score=-r['matches']/segments[(identity,kind)]
                    scores[identity]=min(scores.get(identity,0),score)
        ids &= content_matches | catalog_matches
    sort = params.get('sort','relevance' if query else 'title')
    if sort == 'oldest': key = lambda i:(publications[i].get('edition_year') or 9999, i)
    elif sort == 'newest': key = lambda i:(-(publications[i].get('edition_year') or 0), i)
    elif sort == 'designation': key = lambda i:(publications[i].get('designation') or i, i)
    elif sort == 'relevance' and query: key = lambda i:(i not in catalog_matches, scores.get(i,0), i)
    else: key = lambda i:((publications[i].get('title') or i).casefold(), i)
    ordered = sorted(ids, key=key)
    size = min(100, max(1,int(params.get('size',24))))
    page = max(1,int(params.get('page',1)))
    total, pages = len(ordered), max(1,math.ceil(len(ordered)/size))
    page = min(page, pages)
    selected=ordered[(page-1)*size:page*size]
    if query and scope in ('all','body','toc'):
        for table,kind,locator,ranking in (('passages','pdf','page','bm25(passages,0,0,8,10,5,1)'),('epub_passages','epub','locator','bm25(epub_passages)')):
            targets=[i for i in selected if kind in available[i] and kind in indexed[i] and i in content_matches and len(hits[i])<3]
            if not targets or fmt not in ('both',kind) or (kind=='epub' and scope=='toc'):continue
            match_expr=expr if scope=='all' or kind=='epub' else scope+' : ('+expr+')'
            placeholders=','.join('?' for _ in targets)
            for r in db.execute(f'SELECT rowid,pub_id,{locator},{ranking} AS score FROM {table} WHERE {table} MATCH ? AND pub_id IN ({placeholders}) ORDER BY score',[match_expr,*targets]):
                identity=r['pub_id']
                if len(hits[identity])>=3:continue
                from urllib.parse import quote
                value=r[locator]
                hits[identity].append({'format':kind,locator:int(value) if kind=='pdf' else value,
                    '_rowid':r['rowid'],'url':f'/read/{kind}/{identity}?{locator}='+quote(str(value))})
    rows = []
    for i in selected:
        p = publications[i]
        for hit in hits[i]:
            table,column,match_expr=('passages',5,expr if scope=='all' else scope+' : ('+expr+')') if hit['format']=='pdf' else ('epub_passages',3,expr)
            row=db.execute(f"SELECT snippet({table},{column},char(1),char(2),' ... ',40) FROM {table} WHERE {table} MATCH ? AND rowid=?",(match_expr,hit.pop('_rowid'))).fetchone()
            hit['excerpt']=row[0] if row else ''
        rows.append({k:p.get(k) for k in ('id','title','designation','branch','branch_label','edition_year','edition_date',
                                          'pub_type','collection','functional_category','purpose','page_count','_stub')} |
                    {'files': sorted(available[i]), 'indexed':sorted(indexed[i]), 'hits':hits[i],
                     'editions':len(p.get('editions',[])),
                     'reconstructed':bool(p.get('_reconstructed'))})
    return {'results':rows,'total':total,'page':page,'pages':pages,'size':size,'facets':facets,'available_total':len(available),'catalog_total':len(publications)}
