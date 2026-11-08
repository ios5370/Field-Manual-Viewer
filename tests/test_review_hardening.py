import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import closing, ExitStack
from unittest.mock import patch
from field_manual_viewer import core, discover, indexing, campaign, setup_launcher, catalog


class ReviewHardening(unittest.TestCase):
    def setUp(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=core.ROOT/'tmp');self.root=Path(self.temp.name)
        self.stack=ExitStack()
        for module in (core,discover,indexing,campaign,setup_launcher,catalog):
            self.stack.enter_context(patch.object(module,'ROOT',self.root))
    def tearDown(self):self.stack.close();self.temp.cleanup()

    def test_incomplete_listing_cannot_erase_detail_or_explicit_empty_fields(self):
        full={'id':'one','slug':'one','title':'Full title','purpose':'Purpose','toc':[[1,'Title',1]],'_metadata_level':'detail','_stub':False}
        records={};discover.merge(records,full,'detail')
        for incoming in ({'id':'one','slug':'one'},{'id':'one','slug':'one','title':None,'purpose':'','toc':[]},{'id':'one','slug':'one','title':'Short listing'}):
            discover.merge(records,incoming,'listing')
        self.assertEqual(records['one']['title'],'Full title')
        self.assertEqual(records['one']['purpose'],'Purpose')
        self.assertEqual(records['one']['toc'],full['toc'])
        listing={'id':'listing','slug':'listing','title':'Complete listing title','_stub':False}
        self.assertEqual(discover.merge_metadata(listing,{'title':'Short edition reference','_stub':True})['title'],'Complete listing title')
        discover.persist(records)
        discover.persist({'one':{'id':'one','slug':'one','title':'','purpose':None,'toc':[]}})
        with closing(core.database()) as db:
            saved=json.loads(db.execute('SELECT metadata FROM publications').fetchone()[0])
        self.assertEqual(saved['title'],'Full title');self.assertEqual(saved['toc'],full['toc'])
        discover.merge(records,{**full,'title':'Revised detail'},'new detail')
        self.assertEqual(records['one']['title'],'Revised detail')

    def test_index_report_checkpoints_and_final_summary(self):
        with closing(core.database()) as db,db:
            db.executemany('INSERT INTO files(pub_id,sha256) VALUES (?,?)',[(str(n),'hash') for n in range(51)])
        with patch.object(indexing,'index_one',return_value=[]),patch.object(indexing,'save_json') as save,patch('field_manual_viewer.collect.capacity_guard'):
            indexing.build()
        summaries=[call for call in save.call_args_list if call.args[0].name=='indexing.json']
        self.assertEqual(len(summaries),3)

    def test_storage_plan_recommends_full_library_room_not_just_reserve(self):
        plan=campaign.storage_plan()
        self.assertGreaterEqual(plan['projected_total_bytes'],100_000_000_000)
        self.assertGreater(plan['required_free_bytes'],99_000_000_000)
        self.assertEqual(plan['reserve_bytes'],20_000_000_000)

    def test_setup_parse_failure_is_logged_and_shown(self):
        result=subprocess.CompletedProcess([],1,'','Simulated PowerShell parse failure')
        with patch.object(setup_launcher.subprocess,'run',return_value=result),patch.object(setup_launcher,'show_error') as alert:
            self.assertEqual(setup_launcher.main(),1)
        alert.assert_called_once()
        saved=core.load_json(self.root/'reports/setup-launcher-error.json')
        self.assertIn('parse failure',saved['error'])

    def test_expensive_query_budget_exits_cleanly_with_specific_advice(self):
        with closing(core.database()) as db,db:
            db.executemany('INSERT INTO publications VALUES (?,?)',[(str(i),json.dumps({'id':str(i)})) for i in range(1000)])
        with self.assertRaisesRegex(ValueError,'more specific phrase'):
            catalog.browse({'q':'the'},timeout_seconds=0)
        # Cancellation must release its connection, including on Windows.
        path=self.root/'data/field-manual-viewer.sqlite'
        moved=path.with_suffix('.moved');path.rename(moved);moved.rename(path)
