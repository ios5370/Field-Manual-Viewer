import json
import re
import sqlite3
import time
from contextlib import closing
from .core import ROOT, database, save_json, original_evidence_current

def build_index():
    from .indexing import build
    return build()


def search(query, filters=None, limit=50, scope='all'):
    with closing(database()) as db:
        deadline=time.monotonic()+20
        db.set_progress_handler(lambda: time.monotonic()>deadline,1000)
        try:return _search(db,query,filters,limit,scope)
        except sqlite3.OperationalError as error:
            if str(error)=='interrupted':
                raise ValueError('This search exceeded 20 seconds. Try a more specific phrase or add another word.') from error
            raise


def _search(db,query,filters,limit,scope):
    # Make literal terms safe for FTS syntax; quoted spans remain phrases.
    tokens = re.findall(r'"([^"]+)"|(\S+)', query)
    expr = ' AND '.join('"' + (a or b).replace('"', '""') + '"' for a, b in tokens)
    if not expr:
        db.close()
        return []
    if scope in ('body', 'title', 'toc', 'designation'):
        expr = scope + ' : (' + expr + ')'
    clauses, params = [], [expr]
    for key, value in (filters or {}).items():
        if value and key in ('branch', 'pub_type', 'functional_category', 'collection', 'id'):
            clauses.append(f'json_extract(p.metadata,\'$.{key}\') = ?')
            params.append(value)
    where = ''.join(' AND ' + c for c in clauses)
    params.append(min(max(int(limit), 1), 200))
    rows = db.execute('''SELECT passages.pub_id, passages.page, passages.title,
        passages.designation, snippet(passages,5,'[',']',' ... ',40) AS excerpt,
        bm25(passages,0,0,8,10,5,1) AS rank, p.metadata, f.path, f.sha256, f.bytes, f.status, f.format
        FROM passages JOIN publications p ON p.id=passages.pub_id
        JOIN files f ON f.pub_id=passages.pub_id AND f.format='pdf'
        WHERE f.status IN ('complete','recovered','skipped_verified') AND EXISTS (SELECT 1 FROM index_state i WHERE i.pub_id=f.pub_id AND i.format=f.format AND i.error IS NULL AND i.sha256=f.sha256) AND passages MATCH ?''' + where + ' ORDER BY rank LIMIT ?', params).fetchall()
    evidence={(r['pub_id'],r['format']):dict(r) for r in db.execute('SELECT * FROM file_evidence')}
    result = []
    for row in rows:
        r = dict(row)
        if not original_evidence_current(ROOT,r,evidence.get((r['pub_id'],r['format']),{})):continue
        p = json.loads(r.pop('metadata'))
        r.update(branch=p.get('branch'), collection=p.get('collection'), edition_date=p.get('edition_date'),
                 local_url='/files/' + r['path'] + '#page=' + str(r['page']))
        result.append(r)
    db.close()
    return result
