import html
import io
import json
import threading
import urllib.parse
from .core import ROOT, database, original_evidence_current
from .indexing import epub_sections

PDF_LOCK = threading.RLock()


def archived(pub_id, fmt):
    from contextlib import closing
    with closing(database()) as db:
        r = db.execute("SELECT * FROM files WHERE pub_id=? AND format=? AND sha256 IS NOT NULL AND status IN ('complete','skipped_verified','recovered')", (pub_id,fmt)).fetchone()
        stamp = db.execute('SELECT * FROM file_evidence WHERE pub_id=? AND format=?',(pub_id,fmt)).fetchone()
    if not r or not original_evidence_current(ROOT,r,dict(stamp) if stamp else {}):
        raise FileNotFoundError('Original is missing or changed. Use Library Setup to check saved file integrity.')
    path = (ROOT / r['path']).resolve()
    return path


def pdf_image(pub_id, page, scale=1.4):
    import pypdfium2 as pdfium
    path = archived(pub_id,'pdf')
    with PDF_LOCK:
        doc = pdfium.PdfDocument(path)
        try:
            if page < 1 or page > len(doc): raise ValueError('Page outside document')
            p = doc[page-1]
            bitmap = p.render(scale=min(scale, 4000 / max(p.get_size())))
            img = bitmap.to_pil()
            out = io.BytesIO(); img.save(out,format='PNG')
            bitmap.close(); p.close()
            return out.getvalue()
        finally: doc.close()


def epub_content(pub_id, locator=None):
    sections = list(epub_sections(archived(pub_id,'epub')))
    selected = next((s for s in sections if s[0] == locator), sections[0])
    return {'sections':[{'locator':s[0],'title':s[1]} for s in sections],
            'locator':selected[0],'title':selected[1],'text':selected[2]}
