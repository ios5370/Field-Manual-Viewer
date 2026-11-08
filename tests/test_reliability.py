import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from field_manual_viewer.collect import transfer, validate
from field_manual_viewer.discover import merge
from field_manual_viewer.source import metadata


def epub():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container/>')
        z.writestr('text.xhtml', 'test content ' * 40000)
    return stream.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Type': 'application/epub+zip', 'Content-Length': str(len(body)), 'ETag': '"v1"', **headers}


class FakeClient:
    def __init__(self, body, ignore_range=False):
        self.body, self.ignore_range, self.offsets = body, ignore_range, []

    def open(self, url, headers):
        offset = int(headers.get('Range', 'bytes=0-')[6:-1])
        self.offsets.append(offset)
        if offset and not self.ignore_range:
            return Response(self.body[offset:], 206, **{'Content-Range': f'bytes {offset}-{len(self.body)-1}/{len(self.body)}'})
        return Response(self.body)


class Reliability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (Path(__file__).resolve().parent.parent / "tmp").mkdir(exist_ok=True)

    def test_interruption_range_resume_and_atomic_finalization(self):
        body = epub()
        client = FakeClient(body)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / 'tmp') as d:
            dest = Path(d) / 'test.epub'
            with self.assertRaises(InterruptedError):
                transfer(client, 'https://example.test/a', dest, 'epub', len(body), interrupt_after=1)
            self.assertFalse(dest.exists())
            result = transfer(client, 'https://example.test/a', dest, 'epub', len(body))
            self.assertGreater(result['resumed_from'], 0)
            self.assertEqual(body, dest.read_bytes())

    def test_server_ignoring_range_restarts(self):
        body, client = epub(), FakeClient(epub(), True)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / 'tmp') as d:
            dest = Path(d) / 'test.epub'
            with self.assertRaises(InterruptedError):
                transfer(client, 'https://example.test/a', dest, 'epub', len(body), interrupt_after=1)
            self.assertEqual(transfer(client, 'https://example.test/a', dest, 'epub', len(body))['resumed_from'], 0)
            self.assertEqual(dest.read_bytes(), body)

    def test_invalid_signature_and_size(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / 'tmp') as d:
            p = Path(d) / 'bad.pdf'
            p.write_bytes(b'<html>blocked</html>')
            with self.assertRaises(ValueError): validate(p, 'pdf')
            p.write_bytes(epub())
            with self.assertRaises(ValueError): validate(p, 'epub', 10)

    def test_reject_wrong_mime_and_wrong_range(self):
        client = FakeClient(epub())
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / 'tmp') as d:
            p = Path(d) / 'bad.epub'
            with patch.object(client, 'open', return_value=Response(epub(), **{'Content-Type': 'text/html'})):
                with self.assertRaises(ValueError): transfer(client, 'url', p, 'epub')
            with patch.object(client, 'open', return_value=Response(epub(), 206, **{'Content-Range': 'bytes 7-9/10'})):
                with self.assertRaises(ValueError): transfer(client, 'url', p, 'epub')

    def test_duplicate_missing_metadata_and_editions(self):
        records = {}
        p = {'id': 'one', 'slug': 'one', 'title': None, 'editions': [{'id': 'older', 'slug': 'old'}]}
        merge(records, p, 'a'); merge(records, p, 'b')
        self.assertEqual(len(records), 2)
        self.assertEqual(records['one']['_provenance'], ['a', 'b'])
        self.assertTrue(records['older']['_stub'])
        with self.assertRaises(ValueError): merge(records, {'title': 'missing identity'}, 'a')

    def test_public_metadata_and_no_code_execution(self):
        self.assertEqual(metadata('x {state:{list:{page:2}}} x')['state']['list']['page'], 2)
        with self.assertRaises(ValueError): metadata('<html>challenge</html>')
        with self.assertRaises(ValueError): metadata('{pub:{id:evil()}}')

    @unittest.skip("Optional source-capture audit; not part of a clean source checkout")
    def test_captured_html_serializations(self):
        from field_manual_viewer.core import ROOT
        tested = 0
        for p in (ROOT / 'data/discovery').glob('*.html'):
            text = p.read_text(encoding='utf8')
            if '{state:{' in text or '{pub:{' in text:
                self.assertTrue(metadata(text))
                tested += 1
        self.assertGreater(tested, 2)


if __name__ == '__main__':
    unittest.main()
