from contextlib import closing
import json
from urllib.parse import urlencode
from .core import ROOT, database, save_json
from .network import Client
from .source import metadata

BASE = 'https://www.trex-arms.com/field-manual-vault'


def merge_metadata(old, incoming):
    """Incomplete/list metadata cannot erase richer detail metadata."""
    combined = dict(old)
    lower_priority = ((old.get('_metadata_level') == 'detail' and incoming.get('_metadata_level') != 'detail')
                      or (bool(old.get('title')) and not old.get('_stub') and incoming.get('_stub')))
    for key, value in incoming.items():
        if value is None or value == '' or value == [] or value == {}:
            continue
        if lower_priority and key in old and old[key] not in (None, '', [], {}):
            continue
        combined[key] = value
    combined['_provenance'] = sorted(set(old.get('_provenance', [])) | set(incoming.get('_provenance', [])))
    if old.get('_stub') is False:
        combined['_stub'] = False
    return combined


def merge(records, pub, url):
    if not pub.get('id') or not pub.get('slug'):
        raise ValueError('Missing edition identity/slug')
    old = records.get(pub['id'], {})
    provenance = set(old.get('_provenance', [])) | {url}
    old = merge_metadata(old, pub)
    if pub.get('title'):
        old['_stub'] = False
    old['_document_url'] = BASE + '/' + old['slug']
    old['_provenance'] = sorted(provenance)
    records[pub['id']] = old
    for edition in pub.get('editions', []):
        if edition['id'] not in records:
            records[edition['id']] = {**edition, '_provenance': [url], '_stub': True,
                                      '_document_url': BASE + '/' + edition['slug']}


def discover(refresh=False, on_seed=None, client=None):
    from .parallel_network import SharedClient, NetworkHalted
    from .collect import capacity_guard, CampaignPaused
    client, records, errors, groups = client or SharedClient(), {}, [], []
    client.refresh=refresh
    capacity_guard()
    landing = metadata(client.html(BASE))['landing']
    for p in landing['featured']['pubs']:
        merge(records, p, BASE)
    seed_sent=False
    def checkpoint():
        nonlocal seed_sent
        persist(records)
        eligible=[identity for identity,pub in records.items() if not pub.get('_stub') and pub.get('slug')]
        save_json(ROOT/'reports/discovery.json',{'advertised':landing['stats']['total'],'unique_editions':len(records),'complete':False,'errors':errors,'category_lists':groups})
        save_json(ROOT/'reports/discovery-progress.json',{'message':f'Reading catalog metadata: {len(records):,} entries found of approximately {landing["stats"]["total"]:,}.'})
        if on_seed and not seed_sent and len(eligible)>=20:
            seed_sent=True
            on_seed(eligible[:20])
    checkpoint()
    # Category lists group editions; preserve all explicitly listed edition identities.
    for category, _ in landing['facets']['functional_category']:
        seen, page, expected, listed = set(), 1, None, 0
        try:
            while True:
                capacity_guard();client.check_stopped()
                save_json(ROOT/'reports/discovery-progress.json',{'message':f'Reading catalog metadata: {category.replace(chr(45),chr(32))} ({len(records):,} entries found).'})
                url = BASE + '?' + urlencode({'fn': category, 'page': page})
                d = metadata(client.html(url))
                result = d.get('list')
                if not result or result['page'] != page:
                    raise ValueError('Missing list or pagination mismatch')
                ids = tuple(p['id'] for p in result['results'])
                if not ids or ids in seen:
                    raise ValueError('Empty/repeated pagination before completion')
                seen.add(ids)
                if expected is None:
                    expected = result['total']
                if expected != result['total']:
                    raise ValueError('Catalog total changed during enumeration')
                for p in result['results']:
                    merge(records, p, url)
                listed += len(ids)
                checkpoint()
                print(f'discover {category} page {page}: {listed}/{expected}', flush=True)
                if listed >= expected:
                    if listed != expected:
                        raise ValueError('Pagination count exceeds advertised list total')
                    break
                page += 1
            groups.append({'category': category, 'listed_groups': listed, 'expected_groups': expected})
        except (CampaignPaused,NetworkHalted):raise
        except Exception as e:
            errors.append({'category': category, 'page': page, 'error': str(e)})
        checkpoint()
    advertised = landing['stats']['total']
    report = {'advertised': advertised, 'unique_editions': len(records),
              'difference': len(records) - advertised, 'category_lists': groups, 'errors': errors,
              'complete': len(records) == advertised and not errors,
              'catalog_metadata_records': sum(not p.get('_stub') for p in records.values()),
              'edition_reference_stubs': sum(bool(p.get('_stub')) for p in records.values()),
              'metadata_complete': not any(p.get('_stub') for p in records.values())}
    save_json(ROOT / 'reports/discovery.json', report)
    print(json.dumps(report, indent=2))
    return report


def persist(records):
    with closing(database()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        existing = {r[0]:json.loads(r[1]) for r in db.execute('SELECT id,metadata FROM publications')}
        for key,p in records.items():
            old=existing.get(key,{})
            combined=merge_metadata(old, p)
            if not p.get('_stub'): combined['_stub']=False
            existing[key]=combined
        db.executemany('INSERT OR REPLACE INTO publications VALUES (?,?)',[(p['id'],json.dumps(p)) for p in existing.values()])
    save_json(ROOT/'data/manifest.json',sorted(existing.values(),key=lambda p:p['id']))


def store_detail(pub,url):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',pub['id']):
        raise ValueError('Unsafe edition identity')
    with closing(database()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT metadata FROM publications WHERE id=?',(pub['id'],)).fetchone()
        old=json.loads(row[0]) if row else {}
        records={pub['id']:old}
        # Edition links contain identity only. Load related records first so a
        # short reference cannot overwrite an edition's full catalog metadata.
        for edition in pub.get('editions',[]):
            identity=edition['id']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+',identity):
                raise ValueError('Unsafe related edition identity')
            if identity not in records:
                existing=db.execute('SELECT metadata FROM publications WHERE id=?',(identity,)).fetchone()
                if existing:records[identity]=json.loads(existing[0])
        original={key:json.dumps(value,sort_keys=True) for key,value in records.items()}
        merge(records,{**pub,'_stub':False,'_metadata_level':'detail'},url)
        for p in records.values():
            if original.get(p['id'])==json.dumps(p,sort_keys=True):continue
            db.execute('INSERT OR REPLACE INTO publications VALUES (?,?)',(p['id'],json.dumps(p)))
            save_json(ROOT/'data/metadata'/f'{p["id"]}.json',p)
    return records[pub['id']]


def export_manifest():
    with closing(database()) as db, db:
        records=[json.loads(r[0]) for r in db.execute('SELECT metadata FROM publications ORDER BY id')]
    save_json(ROOT/'data/manifest.json',records)
