import json,hashlib
import unittest
from contextlib import closing
from unittest.mock import patch
import test_test_batch_flow as flow
from field_manual_viewer import core,indexing

class ReaderLookupTests(unittest.TestCase):
    def setUp(self):flow.TestBatchFlow.setUp(self)
    def tearDown(self):flow.TestBatchFlow.tearDown(self)

    def test_existing_index_backfills_row_lookup_without_extracting_pdfs(self):
        with closing(core.database()) as db,db:
            db.execute('INSERT INTO passages(rowid,pub_id,page,title,designation,toc,body) VALUES (101,?,?,?,?,?,?)',('book-00',1,'Book','','','Preserved text'))
            db.execute('INSERT INTO passages(rowid,pub_id,page,title,designation,toc,body) VALUES (999,?,?,?,?,?,?)',('book-00',2,'Book','','','Second page'))
        with patch.object(indexing.pdfium,'PdfDocument',side_effect=AssertionError('No PDF re-extraction')):
            indexing.ensure_pdf_lookup();indexing.ensure_pdf_lookup()
        with closing(core.database()) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT page,passage_rowid FROM pdf_pages ORDER BY page')],[(1,101),(2,999)])
            self.assertEqual(db.execute('SELECT body FROM passages WHERE rowid=999').fetchone()[0],'Second page')

    def _insert_file(self, identity, fmt):
        path = self.root / 'archive' / (identity + '.' + fmt)
        path.write_bytes(self.bodies[fmt])
        sha = hashlib.sha256(self.bodies[fmt]).hexdigest()
        with closing(core.database()) as db, db:
            db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)',
                       (identity, fmt, 'https://example.invalid', str(path.relative_to(self.root)),
                        'complete', path.stat().st_size, sha, None, 'now'))
        return path, sha

    def test_new_documents_do_not_scan_fts_tables_for_deletion(self):
        self._insert_file('book-00', 'pdf')
        self._insert_file('book-00', 'epub')
        statements = []
        db = core.database(); db.set_trace_callback(statements.append)
        with patch.object(indexing, 'database', return_value=db):
            indexing.index_one('book-00')
        self.assertFalse(any('DELETE FROM passages WHERE pub_id' in sql for sql in statements))
        self.assertFalse(any('DELETE FROM epub_passages WHERE pub_id' in sql for sql in statements))
        with closing(core.database()) as check:
            self.assertEqual(check.execute('SELECT count(*) FROM index_rows').fetchone()[0],
                             check.execute('SELECT count(*) FROM passages').fetchone()[0]
                             + check.execute('SELECT count(*) FROM epub_passages').fetchone()[0])

    def test_reindex_deletes_fts_by_rowid_without_duplicates(self):
        self._insert_file('book-00', 'pdf')
        self._insert_file('book-00', 'epub')
        indexing.index_one('book-00')
        statements = []
        db = core.database(); db.set_trace_callback(statements.append)
        with patch.object(indexing, 'database', return_value=db):
            indexing.index_one('book-00', rebuild=True)
        self.assertFalse(any('DELETE FROM passages WHERE pub_id' in sql for sql in statements))
        self.assertFalse(any('DELETE FROM epub_passages WHERE pub_id' in sql for sql in statements))
        with closing(core.database()) as check:
            self.assertEqual(check.execute('SELECT count(*) FROM passages').fetchone()[0], 1)
            self.assertEqual(check.execute('SELECT count(*) FROM epub_passages').fetchone()[0], 1)
            self.assertEqual(check.execute('SELECT count(*) FROM index_rows').fetchone()[0], 2)

    def test_reindex_updates_lookup_with_fts_rows_in_same_transaction(self):
        for identity in ('book-00','book-01'):
            path=self.root/'archive'/(identity+'.pdf');path.write_bytes(self.bodies['pdf'])
            sha=hashlib.sha256(self.bodies['pdf']).hexdigest()
            with closing(core.database()) as db,db:
                db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)',(identity,'pdf','https://example.invalid',str(path.relative_to(self.root)),'complete',path.stat().st_size,sha,None,'now'))
            self.assertTrue(indexing.index_one(identity))
        indexing.ensure_pdf_lookup()
        self.assertTrue(indexing.index_one('book-00',rebuild=True))
        with closing(core.database()) as db:
            rows=list(db.execute('SELECT * FROM pdf_pages'))
            self.assertEqual(len(rows),2)
            for r in rows:
                mapped=db.execute('SELECT pub_id,page FROM passages WHERE rowid=?',(r['passage_rowid'],)).fetchone()
                self.assertEqual(tuple(mapped),(r['pub_id'],r['page']))
