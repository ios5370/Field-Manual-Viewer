"""Targeted, offline recovery of catalog records from preserved source pages."""
import datetime
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from .core import ROOT,database,load_json,save_json,FileLock
from .source import metadata
from .discover import BASE,store_detail,export_manifest


def repair_index_labels(db,publications):
    expected={}
    for pub_id,p in publications.items():
        if not p.get('title'):continue
        headings=defaultdict(list)
        for entry in p.get('toc') or []:
            if isinstance(entry,list) and len(entry)>=3:headings[entry[2]].append(str(entry[1]))
        expected[pub_id]=(p.get('title') or '',p.get('designation') or '',headings)
    changes=defaultdict(list)
    for row in db.execute('SELECT rowid,pub_id,page,title,designation,toc FROM passages'):
        if row['pub_id'] not in expected:continue
        title,designation,headings=expected[row['pub_id']]
        toc=' '.join(headings.get(int(row['page']),[]))
        if (row['title'],row['designation'],row['toc'])!=(title,designation,toc):
            changes[row['pub_id']].append({'rowid':row['rowid'],'page':int(row['page']),
                'before':{'title':row['title'],'designation':row['designation'],'toc':row['toc']},
                'after':{'title':title,'designation':designation,'toc':toc}})
    audit=[]
    for pub_id,rows in changes.items():
        before_hash=hashlib.sha256();after_hash=hashlib.sha256()
        with db:
            for row in rows:
                original=db.execute('SELECT body FROM passages WHERE rowid=?',(row['rowid'],)).fetchone()[0]
                before_hash.update(str(row['page']).encode()+b'\0'+original.encode('utf8'))
                after=row['after']
                db.execute('UPDATE passages SET title=?,designation=?,toc=? WHERE rowid=?',
                    (after['title'],after['designation'],after['toc'],row['rowid']))
                retained=db.execute('SELECT body FROM passages WHERE rowid=?',(row['rowid'],)).fetchone()[0]
                after_hash.update(str(row['page']).encode()+b'\0'+retained.encode('utf8'))
            assert before_hash.digest()==after_hash.digest(),'Body text changed during metadata-only repair'
        audit.append({'id':pub_id,'changed_pages':[r['page'] for r in rows],
            'examples':rows[:3],'body_sha256_before':before_hash.hexdigest(),
            'body_sha256_after':after_hash.hexdigest()})
        print('Index labels repaired:',pub_id,len(rows),'pages; body text unchanged',flush=True)
    return audit


def repair():
    with FileLock('acquisition'),FileLock('index'):
        db=database()
        before={r[0]:json.loads(r[1]) for r in db.execute('SELECT id,metadata FROM publications')}
        suspects={i for i,p in before.items() if not p.get('title')}
        files_before=[tuple(r) for r in db.execute('SELECT * FROM files ORDER BY pub_id,format')]
        acquired={r[0] for r in db.execute("SELECT DISTINCT pub_id FROM files WHERE sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')")}
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup=ROOT/'data/backups'/f'before-metadata-repair-{stamp}.sqlite'
        backup.parent.mkdir(exist_ok=True)
        with sqlite3.connect(backup) as target:db.backup(target)
        db.close()
        cache={};parse_errors=[]
        for path in (ROOT/'data/discovery').glob('*.html'):
            text=path.read_text(encoding='utf8')
            start=text.find('{pub:{')
            if start<0:continue
            identity=re.match(r'\{pub:\{id:"([^"]+)"',text[start:start+256])
            if identity and identity[1] not in suspects:continue
            try:
                pub=metadata(text)['pub']
                if pub['id'] not in suspects or not pub.get('title'):continue
                mtime=path.stat().st_mtime_ns
                if pub['id'] in cache and cache[pub['id']]['mtime']>=mtime:continue
                provenance=load_json(path.with_suffix('.json'),{})
                cache[pub['id']]={'pub':pub,'url':provenance.get('url') or BASE+'/'+pub['slug'],
                    'file':str(path.relative_to(ROOT)),'mtime':mtime}
            except Exception as error:parse_errors.append({'file':str(path.relative_to(ROOT)),'error':str(error)})
        repairs=[]
        for pub_id,entry in cache.items():
            restored=store_detail(entry['pub'],entry['url'])
            repairs.append({'id':pub_id,'before':before[pub_id],'after':restored,'cached_source':entry['file']})
            print('Catalog restored:',pub_id,'->',restored['title'],flush=True)
        db=database()
        after={r[0]:json.loads(r[1]) for r in db.execute('SELECT id,metadata FROM publications')}
        labels=repair_index_labels(db,after)
        files_after=[tuple(r) for r in db.execute('SELECT * FROM files ORDER BY pub_id,format')]
        assert files_before==files_after,'Acquisition ledger changed during metadata repair'
        # A healthy existing catalog record must remain semantically identical.
        untouched=[i for i in before if i not in cache and before[i].get('title')]
        assert all(before[i]==after[i] for i in untouched),'Unrelated catalog metadata changed'
        missing_acquired=sorted(i for i in acquired if not after[i].get('title'))
        result={'passed':not missing_acquired,'backup':str(backup.relative_to(ROOT)),
            'catalog_records_repaired':len(repairs),'acquired_missing_titles_before':sum(i in acquired for i in suspects),
            'acquired_missing_titles_after':len(missing_acquired),'remaining_acquired_missing_titles':missing_acquired,
            'catalog_only_pending_titles':sum(not p.get('title') and i not in acquired for i,p in after.items()),
            'healthy_records_unchanged':len(untouched),'index_documents_repaired':len(labels),
            'index_pages_repaired':sum(len(r['changed_pages']) for r in labels),
            'original_files_and_acquisition_ledger_unchanged':True,'network_requests':0,
            'parse_errors':parse_errors,'catalog_repairs':repairs,'index_repairs':labels}
        save_json(ROOT/'reports/metadata-repair.json',result)
        export_manifest()
        db.close()
        print(json.dumps({k:v for k,v in result.items() if k not in ('catalog_repairs','index_repairs')},indent=2),flush=True)
        return result


if __name__=='__main__':repair()
