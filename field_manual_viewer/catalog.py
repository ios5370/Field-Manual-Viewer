"""Offline catalog queries with publication-level paging and FTS excerpts."""
import collections
import json
import math
import re
import sqlite3
import time
from contextlib import closing
from .indexing import ENGINE
from .core import ROOT, database, load_json, original_evidence_current


def original_available(row, evidence):
    return row['status'] in ('complete','skipped_verified','recovered') and original_evidence_current(
        ROOT, row, evidence.get((row['pub_id'],row['format']), {}))


def file_evidence(db):
    return {(r['pub_id'],r['format']):dict(r) for r in db.execute('SELECT * FROM file_evidence')}


def expression(query):
    return ' AND '.join('"' + (a or b).replace('"', '""') + '"'
                        for a, b in re.findall(r'"([^"]+)"|(\S+)', query))


def status():
    from .indexing import ENGINE
    db = database()
    evidence = file_evidence(db)
    files = [dict(r) for r in db.execute('SELECT * FROM files')]
    indexes = [dict(r) for r in db.execute('SELECT * FROM index_state WHERE error IS NULL')]
    total = db.execute('SELECT count(*) FROM publications').fetchone()[0]
    db.close()
    good={(f['pub_id'],f['format']):f for f in files if original_available(f,evidence)}
    counts={fmt:{'format':fmt,'files':sum(key[1]==fmt for key in good),
                 'bytes':sum(f['bytes'] for key,f in good.items() if key[1]==fmt)} for fmt in ('pdf','epub')}
    indexed=dict(collections.Counter(i['format'] for i in indexes if i['engine']==ENGINE and (i['pub_id'],i['format']) in good
                  and i['sha256']==good[(i['pub_id'],i['format'])]['sha256']))
    acquired=len({key[0] for key in good})
    errors=sum(f['status'] in ('failed','unavailable','detail_failed','missing','damaged') or
               (f['status'] in ('complete','skipped_verified','recovered') and (f['pub_id'],f['format']) not in good) for f in files)
    return {'total': total, 'acquired': acquired, 'formats': counts, 'indexed': indexed,
            'failed_files': errors, 'complete': total>0 and all(counts.get(f, {}).get('files', 0) == total for f in ('pdf','epub')),
            'campaign': load_json(ROOT / 'reports/campaign.json', {'state': 'not_started'}),
            'storage': load_json(ROOT / 'reports/storage-preflight.json', {})}


def details(pub_id):
    db = database()
    evidence = file_evidence(db)
    row = db.execute('SELECT metadata FROM publications WHERE id=?', (pub_id,)).fetchone()
    if not row:
        db.close(); return None
    p = json.loads(row[0])
    p['files'] = [dict(f) for f in db.execute('SELECT * FROM files WHERE pub_id=?', (pub_id,))]
    for file in p['files']:
        file['available'] = original_available(file,evidence)
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
        try:return _browse(db,params)
        except sqlite3.OperationalError as error:
            if str(error)=='interrupted':
                raise ValueError('This search exceeded 20 seconds. Try a more specific phrase or add another word.') from error
            raise


def _browse(db,params):
    evidence = file_evidence(db)
    publications = {r['id']: json.loads(r['metadata']) for r in db.execute('SELECT * FROM publications')}
    available = collections.defaultdict(dict)
    for r in db.execute("SELECT * FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')"):
        if original_available(r,evidence): available[r['pub_id']][r['format']] = dict(r)
    indexed = collections.defaultdict(set)
    segments = {}
    for r in db.execute('SELECT i.pub_id,i.format,i.segments FROM index_state i JOIN files f ON f.pub_id=i.pub_id AND f.format=i.format WHERE i.error IS NULL AND i.sha256=f.sha256 AND i.engine=?',(ENGINE,)):
        indexed[r[0]].add(r[1])
        segments[(r[0],r[1])]=max(1,r[2])
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
                     'editions':len(p.get('editions',[]))})
    return {'results':rows,'total':total,'page':page,'pages':pages,'size':size,'facets':facets,'available_total':len(available),'catalog_total':len(publications)}
