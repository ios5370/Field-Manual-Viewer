"""Markdown reading, indexing and vault-import regression tests."""
import csv
import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from field_manual_viewer import (campaign, catalog, core, discover, import_vault,
                                 indexing, lifecycle, readers, reconcile, report,
                                 search, server_app, setup_control)
from field_manual_viewer.collect import validate
from field_manual_viewer.markdown_text import plain_text, sections


def pdf_bytes():
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({'/Title': 'Demo Manual'})
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
                         '<dc:title>Demo EPUB</dc:title></metadata>'
                         '<manifest><item id="chapter" href="chapter.xhtml"/></manifest>'
                         '<spine><itemref idref="chapter"/></spine></package>')
        archive.writestr('chapter.xhtml', '<html><body><h1>Demo</h1>'
                         '<p>EPUBNeedle local text.</p></body></html>')
    return stream.getvalue()


def md_bytes():
    return ('# Demo Markdown Manual\n\n'
            'Introductory text.\n\n'
            '## Section One\n\n'
            'MarkdownNeedle local recovery text with **emphasis**.\n\n'
            '- first item\n'
            '- second item\n').encode('utf8')


class MarkdownTextTests(unittest.TestCase):
    def test_sections_and_plain_text(self):
        parts = sections(md_bytes().decode())
        self.assertEqual([part['title'] for part in parts], ['Demo Markdown Manual', 'Section One'])
        self.assertIn('MarkdownNeedle', plain_text(parts[1]['text']))
        self.assertNotIn('**', plain_text(parts[1]['text']))


class VaultImportTests(unittest.TestCase):
    def setUp(self):
        (core.ROOT / 'tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=core.ROOT / 'tmp')
        self.root = Path(self.temp.name)
        for folder in ('data', 'reports', 'tmp', 'archive'):
            (self.root / folder).mkdir()
        self.stack = ExitStack()
        for module in (core, lifecycle, catalog, readers, search,
                       server_app, setup_control, indexing, import_vault,
                       discover, report, campaign):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.pdf = pdf_bytes()
        self.epub = epub_bytes()
        self.md = md_bytes()

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def make_vault(self):
        vault = self.root / 'vault'
        bodies = {'pdf': self.pdf, 'epub': self.epub, 'md': self.md}
        row = {
            'id': 'book-00', 'designation': 'FM 1-1', 'title': 'Demo Manual',
            'edition_date': '2025-01-01', 'status': 'active', 'collection': 'current',
            'branch': 'army', 'pub_type': 'FM', 'family': 'Doctrine & Field Manuals',
            'group': 'Demo Subject',
        }
        for fmt, body in bodies.items():
            folder = vault / fmt.upper() / 'Demo Subject'
            folder.mkdir(parents=True)
            zip_path = folder / 'Demo.zip'
            member = 'demo.' + fmt
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(member, body)
            row[f'{fmt}_path'] = f'{fmt.upper()}/Demo Subject/Demo.zip/{member}'
            row[f'sha256_{fmt}'] = hashlib.sha256(body).hexdigest()
        with (vault / 'index.csv').open('w', encoding='utf8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        return vault

    def test_import_index_read_and_remove_markdown(self):
        vault = self.make_vault()
        result = import_vault.import_vault(vault, formats=('pdf', 'epub', 'md'), index=False)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual((self.root / 'archive/book-00/book-00.md').read_bytes(), self.md)
        with closing(core.database()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM files WHERE pub_id='book-00'").fetchone()[0], 3)
        self.assertEqual(validate(self.root / 'archive/book-00/book-00.md', 'md'),
                         hashlib.sha256(self.md).hexdigest())

        indexing.index_one('book-00')
        with closing(core.database()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM md_passages").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM index_state WHERE format='md' AND error IS NULL").fetchone()[0], 1)
        card = catalog.browse({})['results'][0]
        self.assertEqual(card['files'], ['epub', 'md', 'pdf'])
        self.assertEqual(card['indexed'], ['epub', 'md', 'pdf'])
        hit = catalog.browse({'q': 'MarkdownNeedle', 'scope': 'body', 'format': 'md'})
        self.assertEqual(hit['total'], 1)
        self.assertEqual(hit['results'][0]['hits'][0]['format'], 'md')
        content = readers.md_content('book-00', '002-section-one')
        self.assertEqual(content['title'], 'Section One')
        self.assertIn('MarkdownNeedle', content['text'])

        (self.root / 'archive/book-00/book-00.md').unlink()
        reconcile.run_sync(index=False)
        card = catalog.browse({})['results'][0]
        self.assertNotIn('md', card['files'])
        self.assertEqual(card['files'], ['epub', 'pdf'])

    def test_import_is_idempotent(self):
        vault = self.make_vault()
        import_vault.import_vault(vault, formats=('pdf', 'epub', 'md'), index=False)
        result = import_vault.import_vault(vault, formats=('pdf', 'epub', 'md'), index=False)
        self.assertEqual(result['skipped'], 3)
        self.assertEqual(result['files'], 0)

    def test_markdown_only_index_leaves_pdf_and_epub_for_later(self):
        vault = self.make_vault()
        import_vault.import_vault(vault, formats=('pdf', 'epub', 'md'), index=False)
        indexing.index_one('book-00', formats=('md',))
        with closing(core.database()) as db:
            formats = {row[0] for row in db.execute(
                "SELECT format FROM index_state WHERE pub_id='book-00' AND error IS NULL")}
        self.assertEqual(formats, {'md'})
        self.assertEqual(catalog.browse({'q': 'MarkdownNeedle', 'scope': 'body',
                                         'format': 'md'})['total'], 1)

    def test_server_serves_markdown_and_reader_api(self):
        vault = self.make_vault()
        import_vault.import_vault(vault, formats=('pdf', 'epub', 'md'), index=False)
        indexing.index_one('book-00')
        class QuietHandler(server_app.Handler):
            def log_message(self, *args):
                pass
        server = server_app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        server.stop_token = 'known-local-token'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('GET', '/api/md/book-00?locator=002-section-one')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
                self.assertEqual(payload['title'], 'Section One')
                self.assertIn('MarkdownNeedle', payload['text'])
                connection.request('GET', '/files/archive/book-00/book-00.md')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn('text/markdown', response.getheader('Content-Type'))
                self.assertEqual(response.read(), self.md)
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
