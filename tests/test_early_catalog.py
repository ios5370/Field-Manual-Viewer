import collections
import hashlib
import io
import json
import threading
import time
import unittest
from contextlib import closing
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
import test_test_batch_flow as flow
from field_manual_viewer import core,download,discover,network,parallel_network,campaign,lifecycle,collect


class EarlyCatalogTests(unittest.TestCase):
    saved=flow.TestBatchFlow.saved
    def setUp(self):
        flow.TestBatchFlow.setUp(self)
        self.stack.enter_context(patch.object(network,'ROOT',self.root))
        self.stack.enter_context(patch.object(parallel_network,'ROOT',self.root))
        with closing(core.database()) as db,db:db.execute('DELETE FROM publications')
        (self.root/'reports/discovery.json').unlink()
        self.catalog_reads=collections.Counter();self.file_started=threading.Event();self.interrupt=True
        self.file_before_catalog=False

    def tearDown(self):flow.TestBatchFlow.tearDown(self)

    def source(self,mode):
        fixture=self;parent=self.Source
        records=list(self.records.values())
        landing={'featured':{'pubs':records[:10 if mode=='pause_before' else 20]},'facets':{'functional_category':[['one','One']]},'stats':{'total':21}}
        class Source(parent):
            def __init__(self):super().__init__();self.refresh=False
            def check_stopped(self):
                collect.capacity_guard()
                if self.stop.is_set():raise parallel_network.NetworkHalted(self.stop_reason or 'Stopped')
            def html(self,url):return network.Client.html(self,url)
            def open(self,url,headers=None):
                parsed=urlsplit(url)
                if parsed.hostname=='files.trex-arms.com':
                    if not fixture.file_started.is_set():
                        fixture.file_before_catalog=not core.load_json(fixture.root/'reports/discovery.json',{}).get('complete',False)
                    fixture.file_started.set()
                    return super().open(url,headers or {})
                fixture.catalog_reads[url]+=1
                if url==discover.BASE:return io.BytesIO(('{landing:'+json.dumps(landing)+'}').encode())
                if parsed.query:
                    page=int(parse_qs(parsed.query)['page'][0])
                    if mode in ('overlap','pause_after'):
                        if not fixture.file_started.wait(10):raise RuntimeError('Catalog waited but no test transfer started')
                    if ((mode=='pause_before' and page==2) or mode=='pause_after') and fixture.interrupt:
                        fixture.interrupt=False
                        if mode=='pause_after':time.sleep(.1)
                        (fixture.root/'data/pause-acquisition').write_text('Close requested')
                        raise collect.CampaignPaused('Paused during catalog metadata')
                    chosen=(records[:10] if page==1 else records[10:]) if mode=='pause_before' else records
                    result={'page':page,'total':21,'results':chosen}
                    return io.BytesIO(('{state:{},list:'+json.dumps(result)+'}').encode())
                identity=parsed.path.rsplit('/',1)[-1]
                fixture.details[identity]+=1
                return io.BytesIO(('{pub:'+json.dumps(fixture.records[identity])+'}').encode())
        self.stack.enter_context(patch.object(parallel_network,'SharedClient',Source))
        return Source

    def test_files_start_before_full_catalog_and_all_reuses_every_original(self):
        self.source('overlap')
        download.execute(test=True,prepared=True)
        self.assertTrue(self.file_before_catalog)
        self.assertEqual(sum(self.opens.values()),40)
        self.assertTrue(core.load_json(self.root/'reports/discovery.json')['complete'])
        self.assertTrue(lifecycle.inventory()['test_ready'])
        before=self.saved();reads=self.catalog_reads.copy()
        download.execute(test=True,prepared=True)
        self.assertEqual(reads,self.catalog_reads)
        download.execute(prepared=True)
        self.assertEqual(sum(self.opens.values()),42)
        self.assertTrue(all(count==1 for count in self.opens.values()))
        self.assertEqual(before,{p:self.saved()[p] for p in before})

    def test_early_sample_can_replace_exception_from_later_catalog_entries(self):
        self.records['book-00']['epub_url']=None
        self.source('overlap')
        download.execute(test=True,prepared=True)
        report=core.load_json(self.root/'reports/test-download.json')
        self.assertTrue(report['passed'])
        self.assertEqual({r['id'] for r in report['source_exceptions']},{'book-00'})
        self.assertEqual(sum(self.opens.values()),41)
        self.assertTrue(all(n==1 for n in self.opens.values()))

    def test_direct_all_starts_sample_early_then_finishes_rest(self):
        self.source('overlap')
        download.execute(prepared=True)
        self.assertTrue(self.file_before_catalog)
        self.assertEqual(sum(self.opens.values()),42)
        self.assertTrue(all(n==1 for n in self.opens.values()))
        self.assertEqual(lifecycle.inventory()['state'],'complete')

    def test_pause_before_twenty_preserves_catalog_cache_and_resumes(self):
        self.source('pause_before')
        with self.assertRaises(collect.CampaignPaused):download.execute(test=True,prepared=True)
        self.assertEqual(sum(self.opens.values()),0)
        with closing(core.database()) as db:self.assertEqual(db.execute('SELECT count(*) FROM publications').fetchone()[0],10)
        self.assertFalse(core.load_json(self.root/'reports/discovery.json')['complete'])
        initial=self.catalog_reads.copy()
        (self.root/'data/pause-acquisition').unlink()
        download.execute(test=True,prepared=True)
        self.assertEqual(self.catalog_reads[discover.BASE],initial[discover.BASE])
        first=discover.BASE+'?fn=one&page=1'
        self.assertEqual(self.catalog_reads[first],1)
        self.assertEqual(sum(self.opens.values()),40)
        self.assertTrue(lifecycle.inventory()['test_ready'])
        self.assertFalse(any(t.name=='field-manual-catalog' for t in threading.enumerate()))

    def test_pause_during_overlap_then_all_keeps_saved_files(self):
        self.source('pause_after')
        with self.assertRaises(collect.CampaignPaused):download.execute(test=True,prepared=True)
        saved=self.saved();self.assertGreater(len(saved),0)
        self.assertLessEqual(sum(self.opens.values()),40)
        self.assertFalse(core.load_json(self.root/'reports/discovery.json')['complete'])
        (self.root/'data/pause-acquisition').unlink()
        download.execute(prepared=True)
        self.assertEqual(sum(self.opens.values()),42)
        self.assertTrue(all(n==1 for n in self.opens.values()))
        self.assertEqual(saved,{p:self.saved()[p] for p in saved})
        self.assertEqual(lifecycle.inventory()['state'],'complete')

    def test_truncated_legacy_html_cache_is_replaced_once(self):
        source=self.source('overlap')()
        path=self.root/'data/discovery'/(hashlib.sha256(discover.BASE.encode()).hexdigest()[:24]+'.html')
        path.parent.mkdir(parents=True);path.write_text('{landing:')
        text=source.html(discover.BASE)
        self.assertIn('featured',text)
        self.assertEqual(source.html(discover.BASE),text)
        self.assertEqual(self.catalog_reads[discover.BASE],1)
        self.assertEqual(list(path.parent.glob('*.tmp')),[])

    def test_incomplete_catalog_allows_only_saved_twenty_not_bulk(self):
        core.save_json(self.root/'reports/discovery.json',{'complete':False})
        ids=list(self.records)[:20];core.save_json(self.root/'data/test-selection.json',ids)
        with self.assertRaises(RuntimeError):campaign.ensure_bulk_ready()
        with self.assertRaises(ValueError):campaign.ensure_bulk_ready(test_sample=True,only_ids=list(self.records),limit=20)
        campaign.ensure_bulk_ready(test_sample=True,only_ids=ids,limit=20)
