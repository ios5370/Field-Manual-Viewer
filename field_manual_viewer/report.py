from contextlib import closing
import collections
import json
import csv
from .core import ROOT, database, save_json,load_json


def report():
    with closing(database()) as db, db:
        files = [dict(r) for r in db.execute('SELECT * FROM files')]
        extracted = [dict(r) for r in db.execute('SELECT * FROM extraction')]
        passages = db.execute('SELECT COUNT(*) FROM passages').fetchone()[0]
        pubs = db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]
    good = [f for f in files if f['sha256'] and (ROOT / f['path']).exists()]
    by_format = {fmt: {'files': len([f for f in good if f['format'] == fmt]),
                      'bytes': sum(f['bytes'] for f in good if f['format'] == fmt)} for fmt in ('pdf', 'epub')}
    checksums = collections.defaultdict(list)
    for f in good:
        checksums[f['sha256']].append(f['path'])
    sample = json.loads((ROOT / 'data/sample.json').read_text()) if (ROOT / 'data/sample.json').exists() else []
    with closing(database()) as db, db:
        all_ids=[r[0] for r in db.execute('SELECT id FROM publications')]
    expected = {(p, fmt) for p in all_ids for fmt in ('pdf', 'epub')}
    outstanding = sorted(expected - {(f['pub_id'],f['format']) for f in good})
    result = {'catalog_editions': pubs, 'sample_editions': len(sample), 'formats': by_format,
              'statuses': dict(collections.Counter(f['status'] for f in files)),
              'indexed_documents': len([e for e in extracted if not e['error']]), 'indexed_pages': passages,
              'sparse_pages': sum(e['sparse_pages'] or 0 for e in extracted),
              'duplicates': [paths for paths in checksums.values() if len(paths) > 1],
              'outstanding': outstanding, 'failed': [f for f in files if f['error']],
              'index_bytes': (ROOT / 'data/field-manual-viewer.sqlite').stat().st_size,
              'download_requested_by_user': True}
    if good and sample:
        total = sum(f['bytes'] for f in good if f['pub_id'] in sample)
        n = len({f['pub_id'] for f in good if f['pub_id'] in sample})
        result['sample_archive_bytes'] = total
        result['naive_4215_projection_bytes'] = round(total / n * 4215)
        result['projection_warning'] = 'Purposive sample, not a statistically representative whole-library estimate.'
    save_json(ROOT / 'reports/summary.json', result)
    lookup={(f['pub_id'],f['format']):f for f in files}
    path=ROOT/'reports/acquisition-completeness.csv'
    tmp=path.with_suffix('.csv.tmp')
    with tmp.open('w',newline='',encoding='utf8') as out:
        writer=csv.DictWriter(out,fieldnames=['pub_id','format','status','bytes','sha256','path','error'])
        writer.writeheader()
        for pub_id in all_ids:
            for fmt in ('pdf','epub'):
                row=lookup.get((pub_id,fmt),{'status':'not_processed'})
                writer.writerow({k:(pub_id if k=='pub_id' else fmt if k=='format' else row.get(k)) for k in writer.fieldnames})
    tmp.replace(path)
    return result
