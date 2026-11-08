"""Run real transfer validation and indexing with a deterministic local source."""
import collections
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
from field_manual_viewer import core, lifecycle, download, campaign, parallel, collect, discover, indexing, report, setup_control


def documents():
    pdf=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(pdf)
    epub=io.BytesIO()
    with zipfile.ZipFile(epub,'w') as z:
        z.writestr('mimetype','application/epub+zip')
        z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        z.writestr('book.opf','<package><manifest><item id="chapter" href="chapter.xhtml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
        z.writestr('chapter.xhtml','<html><body><h1>Sample</h1><p>Searchable local acceptance text.</p></body></html>')
    return {'pdf':pdf.getvalue(),'epub':epub.getvalue()}


class TestBatchFlow(unittest.TestCase):
    def setUp(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=core.ROOT/'tmp');self.root=Path(self.temp.name)
        for folder in ('data','reports','tmp','archive'):(self.root/folder).mkdir()
        self.stack=ExitStack()
        for module in (core,lifecycle,download,campaign,parallel,collect,discover,indexing,report,setup_control):
            self.stack.enter_context(patch.object(module,'ROOT',self.root))
        self.bodies=documents();self.opens=collections.Counter();self.details=collections.Counter();self.counter_lock=threading.Lock()
        self.records={}
        for n in range(21):
            identity=f'book-{n:02}'
            self.records[identity]={'id':identity,'slug':identity,'title':f'Book {n}',
                'branch':'army','edition_year':2000+n,'toc':[],'editions':[],
                'pdf_url':f'https://files.trex-arms.com/{identity}.pdf',
                'epub_url':f'https://files.trex-arms.com/{identity}.epub',
                'assets':[{'format':fmt,'file_size':len(body)} for fmt,body in self.bodies.items()]}
        with closing(core.database()) as db,db:
            db.executemany('INSERT INTO publications VALUES (?,?)',[(i,json.dumps(p)) for i,p in self.records.items()])
        core.save_json(self.root/'reports/discovery.json',{'complete':True})
        fixture=self
        class LocalSource:
            def __init__(self):self.stop=threading.Event();self.stop_reason=None
            def check_stopped(self):
                if self.stop.is_set():raise RuntimeError(self.stop_reason)
            def halt(self,reason):self.stop_reason=str(reason);self.stop.set()
            def metrics(self):return {'requests':{},'http_errors':{}}
            def html(self,url):
                identity=url.rsplit('/',1)[-1]
                fixture.details[identity]+=1
                return '{pub:'+json.dumps(fixture.records[identity])+'}'
            def open(self,url,headers):
                with fixture.counter_lock:fixture.opens[url]+=1
                fmt=url.rsplit('.',1)[-1];body=fixture.bodies[fmt]
                response=io.BytesIO(body);response.status=200
                response.headers={'Content-Type':'application/pdf' if fmt=='pdf' else 'application/epub+zip',
                    'Content-Length':str(len(body)),'ETag':'"local-test"'}
                return response
        self.Source=LocalSource
        self.stack.enter_context(patch.object(parallel,'SharedClient',LocalSource))

    def tearDown(self):self.stack.close();self.temp.cleanup()

    def saved(self):
        with closing(core.database()) as db:
            rows=list(db.execute("SELECT path,sha256 FROM files WHERE status IN ('complete','recovered','skipped_verified')"))
        return {row[0]:(row[1],(self.root/row[0]).stat().st_mtime_ns) for row in rows}

    def test_sample_then_repeat_then_all_retains_every_sample_original(self):
        download.execute(test=True,prepared=True)
        self.assertEqual(sum(self.opens.values()),40)
        self.assertTrue(lifecycle.inventory()['test_ready'])
        job=core.load_json(self.root/'reports/campaign.json')
        self.assertEqual((job['saved_files'],job['expected_files'],job['scope_publications']),(40,40,20))
        self.assertIn('PDF + EPUB files saved for 20 publications',job['message'])
        saved=self.saved();details_before=self.details.copy()
        download.execute(test=True,prepared=True)
        self.assertEqual(sum(self.opens.values()),40)
        self.assertEqual(self.details,details_before)
        download.execute(prepared=True)
        self.assertEqual(sum(self.opens.values()),42)
        self.assertTrue(all(count==1 for count in self.opens.values()))
        self.assertEqual({path:self.saved()[path] for path in saved},saved)
        state=lifecycle.inventory()
        self.assertEqual(state['state'],'complete')
        self.assertEqual((state['pdf_indexed'],state['epub_indexed']),(21,21))

    def test_isolated_missing_epub_uses_replacement_and_all_does_not_repeat(self):
        self.records['book-00']['epub_url']=None
        with closing(core.database()) as db,db:
            db.execute('UPDATE publications SET metadata=? WHERE id=?',(json.dumps(self.records['book-00']),'book-00'))
        download.execute(test=True,prepared=True)
        outcome=core.load_json(self.root/'reports/test-download.json')
        self.assertTrue(outcome['passed'])
        self.assertEqual({r['id'] for r in outcome['source_exceptions']},{'book-00'})
        self.assertNotIn('book-00',outcome['editions'])
        self.assertTrue(lifecycle.inventory()['test_ready'])
        before=self.opens.copy()
        download.execute(prepared=True)
        self.assertEqual(self.opens,before)
        self.assertEqual(sum(self.opens.values()),41)
        self.assertTrue(all(n==1 for n in self.opens.values()))

    def test_widespread_missing_assets_still_blocks_full_download(self):
        for identity in ('book-00','book-01','book-02'):
            self.records[identity]['epub_url']=None
            with closing(core.database()) as db,db:
                db.execute('UPDATE publications SET metadata=? WHERE id=?',(json.dumps(self.records[identity]),identity))
        with self.assertRaisesRegex(RuntimeError,'did not validate'):
            download.execute(prepared=True)
        self.assertFalse(core.load_json(self.root/'reports/test-download.json')['passed'])
        self.assertLess(sum(self.opens.values()),40)

    def test_download_all_works_without_clicking_test_first(self):
        download.execute(prepared=True)
        self.assertEqual(sum(self.opens.values()),42)
        self.assertTrue(all(count==1 for count in self.opens.values()))
        self.assertEqual(lifecycle.inventory()['state'],'complete')


if __name__=='__main__':unittest.main()
