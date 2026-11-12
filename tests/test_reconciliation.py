"""Core archive/database recovery regression tests."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from field_manual_viewer import (catalog, core, indexing, lifecycle, readers, reconcile,
                                 search, server_app, setup_control)


def pdf_bytes(title='Recovered Manual'):
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({'/Title': title})
    writer.write(stream)
    return stream.getvalue()


def epub_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
                         '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr('book.opf',
                         '<package><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         '<dc:title>Recovered EPUB</dc:title></metadata>'
                         '<manifest><item id="chapter" href="chapter.xhtml"/></manifest>'
                         '<spine><itemref idref="chapter"/></spine></package>')
        archive.writestr('chapter.xhtml', '<html><body><h1>Sample</h1>'
                         '<p>Searchable local recovery text.</p></body></html>')
    return stream.getvalue()


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        (core.ROOT / 'tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=core.ROOT / 'tmp')
        self.root = Path(self.temp.name)
        for folder in ('data', 'reports', 'tmp', 'archive'):
            (self.root / folder).mkdir()
        self.stack = ExitStack()
        for module in (core, lifecycle, catalog, readers, search, setup_control,
                       indexing, server_app):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.pdf = pdf_bytes()
        self.epub = epub_bytes()

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def write(self, pub_id, fmt, body):
        path = self.root / 'archive' / pub_id / f'{pub_id}.{fmt}'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def rows(self):
        with closing(core.database()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM files')]

    def test_archive_only_recovery(self):
        self.write('book', 'pdf', self.pdf)
        self.write('book', 'epub', self.epub)
        with patch('socket.socket.connect', side_effect=AssertionError('network')):
            report = reconcile.run_sync(index=True)
        self.assertEqual(report['state'], 'idle')
        with closing(core.database()) as db:
            publications = {row[0]: json.loads(row[1]) for row in db.execute('SELECT * FROM publications')}
            indexes = {row[0] for row in db.execute('SELECT format FROM index_state WHERE error IS NULL')}
        self.assertTrue(publications['book']['_reconstructed'])
        self.assertEqual(indexes, {'pdf', 'epub'})
        self.assertEqual({row['format'] for row in self.rows()}, {'pdf', 'epub'})
        self.assertEqual(catalog.browse({})['total'], 1)
        self.assertEqual(catalog.browse({'q': 'Searchable', 'scope': 'body'})['total'], 1)
        self.assertEqual(readers.archived('book', 'pdf').read_bytes(), self.pdf)
        self.assertEqual(lifecycle.inventory()['state'], 'complete')

    def test_database_loss_reconstructs(self):
        self.write('book', 'pdf', self.pdf)
        self.write('book', 'epub', self.epub)
        reconcile.run_sync(index=True)
        database = self.root / 'data/field-manual-viewer.sqlite'
        for suffix in ('', '-wal', '-shm'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        reconcile.run_sync(index=True)
        with closing(core.database()) as db:
            publication = json.loads(db.execute("SELECT metadata FROM publications WHERE id='book'").fetchone()[0])
        self.assertTrue(publication['_reconstructed'])
        self.assertEqual(catalog.browse({})['total'], 1)

    def test_removed_file_keeps_last_known_good_and_reader_boundary(self):
        pdf = self.write('book', 'pdf', self.pdf)
        self.write('book', 'epub', self.epub)
        reconcile.run_sync(index=True)
        pdf.unlink()
        with patch.object(catalog, 'original_evidence_current',
                          side_effect=AssertionError('catalog must use cached presence')):
            card = next(row for row in catalog.browse({})['results'] if row['id'] == 'book')
        self.assertEqual(card['files'], ['epub', 'pdf'])
        with self.assertRaises(FileNotFoundError):
            readers.archived('book', 'pdf')
        reconcile.run_sync(index=True)
        card = next(row for row in catalog.browse({})['results'] if row['id'] == 'book')
        self.assertEqual(card['files'], ['epub'])
        with closing(core.database()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM index_state WHERE pub_id='book'").fetchone()[0], 2)

    def test_disconnected_archive_preserves_catalog(self):
        self.write('book', 'pdf', self.pdf)
        self.write('book', 'epub', self.epub)
        reconcile.run_sync(index=True)
        archive = self.root / 'archive'
        moved = self.root / 'archive-offline'
        archive.rename(moved)
        try:
            report = reconcile.run_sync(index=False)
            self.assertFalse(report['archive_available'])
            self.assertEqual(catalog.browse({})['total'], 1)
            with self.assertRaises(FileNotFoundError):
                readers.archived('book', 'pdf')
        finally:
            moved.rename(archive)
        self.assertTrue(reconcile.run_sync(index=False)['archive_available'])

    def test_same_size_replacement_invalidates_index(self):
        path = self.write('book', 'pdf', self.pdf)
        reconcile.run_sync(index=True)
        with closing(core.database()) as db:
            old = dict(db.execute("SELECT * FROM files WHERE pub_id='book' AND format='pdf'").fetchone())
            old_index = dict(db.execute("SELECT * FROM index_state WHERE pub_id='book' AND format='pdf'").fetchone())
        replacement = pdf_bytes('Recovered Manua1')
        self.assertEqual(len(replacement), len(self.pdf))
        stamp = path.stat().st_mtime_ns
        path.write_bytes(replacement)
        os.utime(path, ns=(stamp + 10_000_000, stamp + 10_000_000))
        reconcile.run_sync(index=True)
        with closing(core.database()) as db:
            current = dict(db.execute("SELECT * FROM files WHERE pub_id='book' AND format='pdf'").fetchone())
            current_index = dict(db.execute("SELECT * FROM index_state WHERE pub_id='book' AND format='pdf'").fetchone())
        self.assertNotEqual(current['sha256'], old['sha256'])
        self.assertEqual(current_index['sha256'], current['sha256'])
        self.assertNotEqual(current_index['sha256'], old_index['sha256'])


if __name__ == '__main__':
    unittest.main()
