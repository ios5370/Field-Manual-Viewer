"""Fast embedded-OCR and EPUB-spine extraction; all outputs are rebuildable."""
from contextlib import closing
import json
import posixpath
import zipfile
import xml.etree.ElementTree as ET
import pypdfium2 as pdfium
from bs4 import BeautifulSoup
from .core import ROOT, database, save_json, FileLock

ENGINE = 'pdfium-5.15.1-epub-spine-v1'


def _epub_xml(data):
    if b'<!entity' in data.lower():
        raise ValueError('EPUB XML entity declarations are not allowed')
    return ET.fromstring(data)


def epub_sections(path):
    with zipfile.ZipFile(path) as z:
        from .collect import validate_epub_container
        validate_epub_container(z)
        container = _epub_xml(z.read('META-INF/container.xml'))
        rootfile = next(e for e in container.iter() if e.tag.endswith('rootfile')).attrib['full-path']
        package = _epub_xml(z.read(rootfile))
        items = {e.attrib['id']: e.attrib for e in package.iter() if e.tag.endswith('}item') or e.tag == 'item'}
        for itemref in package.iter():
            if not (itemref.tag.endswith('}itemref') or itemref.tag == 'itemref'):
                continue
            item = items.get(itemref.attrib.get('idref'))
            if not item:
                continue
            href = posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), item['href'].split('#')[0]))
            if href.startswith('../') or href.startswith('/'):
                raise ValueError('Unsafe EPUB spine path')
            soup = BeautifulSoup(z.read(href), 'html.parser')
            for tag in soup(['script', 'style', 'iframe', 'object']):
                tag.decompose()
            title = soup.find(['h1', 'h2', 'title'])
            yield href, title.get_text(' ', strip=True) if title else href, soup.get_text(' ', strip=True)


def ensure_pdf_lookup():
    """Map existing FTS row IDs once, without re-extracting any PDF text."""
    with closing(database()) as db:
        if db.execute("SELECT 1 FROM reader_lookup WHERE version='pdf-pages-v1'").fetchone():return
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM reader_lookup WHERE version='pdf-pages-v1'").fetchone():return
            db.execute('INSERT OR REPLACE INTO pdf_pages SELECT pub_id,CAST(page AS INTEGER),rowid FROM passages')
            db.execute("INSERT INTO reader_lookup VALUES ('pdf-pages-v1')")


def _delete_index_rows(db, table, pub_id, fmt, prior):
    """Delete FTS rows by rowid; never scan an UNINDEXED pub_id for new docs."""
    rowids = [r[0] for r in db.execute(
        'SELECT rowid FROM index_rows WHERE pub_id=? AND format=?', (pub_id, fmt))]
    if not rowids and fmt == 'pdf':
        rowids = [r[0] for r in db.execute(
            'SELECT passage_rowid FROM pdf_pages WHERE pub_id=?', (pub_id,))]
    if rowids:
        db.executemany(f'DELETE FROM {table} WHERE rowid=?', [(rowid,) for rowid in rowids])
    elif prior is not None:
        # Legacy index without row mapping.  Reindexing is rare; this fallback
        # is deliberately avoided for newly downloaded documents.
        db.execute(f'DELETE FROM {table} WHERE pub_id=?', (pub_id,))
    db.execute('DELETE FROM index_rows WHERE pub_id=? AND format=?', (pub_id, fmt))


def index_one(pub_id, rebuild=False):
    db = database()
    p = json.loads(db.execute('SELECT metadata FROM publications WHERE id=?', (pub_id,)).fetchone()[0])
    files = db.execute("SELECT * FROM files WHERE pub_id=? AND sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')", (pub_id,)).fetchall()
    results = []
    for f in files:
        prior = db.execute('SELECT * FROM index_state WHERE pub_id=? AND format=?', (pub_id, f['format'])).fetchone()
        if not rebuild and prior and not prior['error'] and prior['sha256'] == f['sha256'] and prior['engine'] == ENGINE:
            continue
        sparse, rows = 0, []
        try:
            if f['format'] == 'pdf':
                doc = pdfium.PdfDocument(ROOT / f['path'])
                try:
                    for i in range(len(doc)):
                        page = doc[i]
                        tp = page.get_textpage()
                        text = tp.get_text_bounded()
                        tp.close(); page.close()
                        sparse += len(text.strip()) < 40
                        headings = ' '.join(str(t[1]) for t in p.get('toc', []) if isinstance(t, list) and len(t) >= 3 and t[2] == i + 1)
                        rows.append((pub_id, i + 1, p.get('title') or '', p.get('designation') or '', headings, text))
                finally:
                    doc.close()
                with db:
                    _delete_index_rows(db, 'passages', pub_id, 'pdf', prior)
                    last=db.execute('SELECT rowid FROM passages ORDER BY rowid DESC LIMIT 1').fetchone()
                    first=(last[0] if last else 0)+1
                    db.executemany('INSERT INTO passages(rowid,pub_id,page,title,designation,toc,body) VALUES (?,?,?,?,?,?,?)',[(first+n,*row) for n,row in enumerate(rows)])
                    db.executemany('INSERT INTO index_rows VALUES (?,?,?)',[(pub_id,'pdf',first+n) for n in range(len(rows))])
                    db.execute('DELETE FROM pdf_pages WHERE pub_id=?',(pub_id,))
                    db.executemany('INSERT INTO pdf_pages VALUES (?,?,?)',[(pub_id,row[1],first+n) for n,row in enumerate(rows)])
                    db.execute('INSERT OR REPLACE INTO extraction VALUES (?,?,?,NULL)', (pub_id, len(rows), sparse))
            else:
                rows = [(pub_id, loc, title, text) for loc, title, text in epub_sections(ROOT / f['path'])]
                if not rows:
                    raise ValueError('EPUB has no readable spine sections')
                with db:
                    _delete_index_rows(db, 'epub_passages', pub_id, 'epub', prior)
                    last=db.execute('SELECT rowid FROM epub_passages ORDER BY rowid DESC LIMIT 1').fetchone()
                    first=(last[0] if last else 0)+1
                    db.executemany('INSERT INTO epub_passages(rowid,pub_id,locator,title,body) VALUES (?,?,?,?,?)',
                                   [(first+n,*row) for n,row in enumerate(rows)])
                    db.executemany('INSERT INTO index_rows VALUES (?,?,?)',
                                   [(pub_id,'epub',first+n) for n in range(len(rows))])
            with db:
                db.execute('INSERT OR REPLACE INTO index_state VALUES (?,?,?,?,?,?,NULL)',
                           (pub_id, f['format'], f['sha256'], ENGINE, len(rows), sparse))
            results.append({'id': pub_id, 'format': f['format'], 'segments': len(rows), 'sparse': sparse, 'status': 'indexed'})
        except Exception as e:
            with db:
                db.execute('INSERT OR REPLACE INTO index_state VALUES (?,?,?,?,0,0,?)',
                           (pub_id, f['format'], f['sha256'], ENGINE, str(e)))
            results.append({'id': pub_id, 'format': f['format'], 'status': 'failed', 'error': str(e)})
    db.close()
    return results


def build(rebuild=False):
    results = []
    with FileLock('index'):
        with closing(database()) as db:
            ids = [r[0] for r in db.execute('SELECT DISTINCT pub_id FROM files WHERE sha256 IS NOT NULL')]
        for n,pub_id in enumerate(ids,1):
            from .collect import capacity_guard
            capacity_guard()
            save_json(ROOT/'reports/indexing-progress.json',{'message':f'Preparing document {n} / {len(ids)}: {pub_id}'})
            for result in index_one(pub_id, rebuild):
                results.append(result)
                print(result, flush=True)
            if n % 25 == 0:
                save_json(ROOT / 'reports/indexing.json', results)
        save_json(ROOT / 'reports/indexing.json', results)
    return results
