import os
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch
from field_manual_viewer import core, lifecycle, collect, setup_control, catalog, readers, search, reconcile
from field_manual_viewer.indexing import ENGINE


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        base=core.ROOT/'tmp'; base.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.root=Path(self.temp.name)
        for folder in ('data','reports','archive','tmp'):(self.root/folder).mkdir()
        (self.root/'reports/discovery.json').write_text('{"complete":true}')
        self.patches=ExitStack()
        for module in (core,lifecycle,collect,setup_control,catalog,readers,search):
            self.patches.enter_context(patch.object(module,'ROOT',self.root))
        with closing(core.database()) as db,db:
            db.execute('INSERT INTO publications VALUES (?,?)',('book',json.dumps({'id':'book','title':'Test manual'})))

    def tearDown(self):
        self.patches.close(); self.temp.cleanup()

    def original(self,fmt):
        path=self.root/'archive'/('book.'+fmt);path.write_bytes(b'original document bytes')
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        with closing(core.database()) as db,db:
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)',('book',fmt,'https://source.invalid',str(path.relative_to(self.root)),'complete',path.stat().st_size,sha,None,'now'))
            db.execute('INSERT INTO file_evidence VALUES (?,?,?,?,?)',('book',fmt,sha,path.stat().st_size,path.stat().st_mtime_ns))
            db.execute('INSERT INTO index_state VALUES (?,?,?,?,?,?,?)',('book',fmt,sha,ENGINE,1,0,None))
        return path,sha

    def test_completion_reconciles_actual_files_instead_of_marker(self):
        path,_=self.original('pdf');self.original('epub')
        self.assertEqual(lifecycle.inventory()['state'],'complete')
        self.assertTrue(lifecycle.finalize()['missing_files']==0)
        path.unlink()
        self.assertNotEqual(lifecycle.inventory()['state'],'complete')
        lifecycle.finalize()
        self.assertFalse(json.loads((self.root/'data/completion.json').read_text())['ready'])

    def test_same_size_damage_requires_check_and_preserves_saved_hash(self):
        path,sha=self.original('pdf');self.original('epub')
        with closing(core.database()) as db,db:
            db.execute('INSERT INTO passages VALUES (?,?,?,?,?,?)',('book',1,'Test manual','FM 1','','distinctive passage'))
        self.assertEqual(len(search.search('distinctive')),1)
        stamp=path.stat().st_mtime_ns
        path.write_bytes(b'X'*path.stat().st_size)
        os.utime(path,ns=(stamp+10000000,stamp+10000000))
        # Cached-first browsing keeps the last known good card while search and
        # the reader already reject the changed bytes.
        self.assertEqual(catalog.browse({})['results'][0]['files'],['epub','pdf'])
        self.assertTrue(next(f for f in catalog.details('book')['files'] if f['format']=='pdf')['available'])
        self.assertEqual(catalog.browse({'q':'distinctive','scope':'body'})['total'],0)
        self.assertEqual(search.search('distinctive'),[])
        with self.assertRaises(FileNotFoundError):readers.archived('book','pdf')
        self.assertEqual(lifecycle.inventory()['state'],'verify_pending')
        issues=lifecycle.reconcile_hashes()
        self.assertEqual(len(issues),1)
        with closing(core.database()) as db:
            row=db.execute("SELECT sha256,status FROM files WHERE format='pdf'").fetchone()
        self.assertEqual(tuple(row),(sha,'damaged'))
        self.assertEqual(catalog.browse({})['results'][0]['files'],['epub'])
        self.assertFalse(next(f for f in catalog.details('book')['files'] if f['format']=='pdf')['available'])
        self.assertEqual(path.read_bytes(),b'X'*path.stat().st_size)

    def test_unavailable_original_is_explicit_exception(self):
        self.original('epub')
        with closing(core.database()) as db,db:
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)',('book','pdf',None,'archive/book.pdf','unavailable',None,None,'No public asset','now'))
        state=lifecycle.inventory()
        self.assertEqual(state['state'],'exceptions')
        self.assertEqual(state['pdf_verified'],0)
        self.assertEqual(state['source_exceptions'],1)
        self.assertEqual(state['missing_files'],1)

    def test_index_completion_is_separate_from_saved_originals(self):
        self.original('pdf');self.original('epub')
        with closing(core.database()) as db,db:db.execute("DELETE FROM index_state WHERE format='pdf'")
        state=lifecycle.inventory()
        self.assertEqual(state['state'],'index_pending')
        self.assertEqual(state['pdf_verified'],1)
        self.assertEqual(state['pdf_indexed'],0)

    def test_second_start_reconnects_without_spawning_another_worker(self):
        with core.FileLock('setup-job'),patch.object(setup_control.subprocess,'Popen') as spawn:
            result=setup_control.start_job('download',authorized=True)
        self.assertFalse(result['started']);spawn.assert_not_called()

    def test_bulk_start_requires_explicit_authorization(self):
        with self.assertRaises(ValueError):setup_control.start_job('download')

    def test_missing_or_invalid_action_cannot_start_acquisition(self):
        with patch.object(setup_control.subprocess,'Popen') as spawn:
            for action in (None,'','unknown'):
                with self.assertRaises(ValueError):setup_control.start_job(action,authorized=True)
                with self.assertRaises(ValueError):setup_control.worker(action)
            spawn.assert_not_called()

    def test_insufficient_space_stops_before_discovery(self):
        from field_manual_viewer import download
        with patch.object(download,'ROOT',self.root),patch('field_manual_viewer.collect.shutil.disk_usage') as space,patch('field_manual_viewer.discover.discover') as discover:
            space.return_value.free=19_000_000_000
            with self.assertRaises(collect.CampaignPaused):download.execute(prepared=True)
            discover.assert_not_called()

    def test_completed_invocation_and_status_remain_offline(self):
        from field_manual_viewer import download
        self.original('pdf');self.original('epub')
        with patch.object(download,'ROOT',self.root),patch('socket.socket.connect',side_effect=AssertionError('Unexpected network request')):
            setup_control.worker('download')
            state=lifecycle.inventory()
        self.assertEqual(state['state'],'complete')
        self.assertTrue(json.loads((self.root/'data/completion.json').read_text())['ready'])

    def test_repair_is_scoped_to_recorded_problems_not_the_unacquired_catalog(self):
        from field_manual_viewer import download
        self.original('epub')
        with closing(core.database()) as db,db:
            db.execute('INSERT INTO publications VALUES (?,?)',('new-book',json.dumps({'id':'new-book'})))
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)',('book','pdf',None,'archive/book.pdf','unavailable',None,None,'No public asset','now'))
        with patch.object(download,'ROOT',self.root),patch('field_manual_viewer.campaign.run',return_value={'state':'complete_with_failures'}) as run:
            download.execute(retry_failed=True,prepared=True)
        self.assertEqual(run.call_args.kwargs['only_ids'],['book'])
        self.assertTrue(run.call_args.kwargs['retry_failed'])

    def test_deleted_format_disappears_from_links_and_text_hits(self):
        pdf,_=self.original('pdf');epub,_=self.original('epub')
        with closing(core.database()) as db,db:
            db.execute('INSERT INTO passages VALUES (?,?,?,?,?,?)',('book',1,'Test manual','FM 1','','distinctive passage'))
        self.assertEqual(catalog.browse({'q':'distinctive','scope':'body','format':'pdf'})['total'],1)
        pdf.unlink()
        self.assertEqual(catalog.browse({})['results'][0]['files'],['epub','pdf'])
        self.assertTrue(catalog.status()['complete'])
        self.assertTrue(next(f for f in catalog.details('book')['files'] if f['format']=='pdf')['available'])
        reconcile.run_sync(index=False)
        self.assertFalse(catalog.status()['complete'])
        self.assertEqual(catalog.status()['formats']['pdf']['files'],0)
        state=catalog.browse({})
        self.assertEqual(state['results'][0]['files'],['epub'])
        self.assertEqual(catalog.browse({'q':'distinctive','scope':'body','format':'both'})['total'],0)
        self.assertFalse(next(f for f in catalog.details('book')['files'] if f['format']=='pdf')['available'])
        epub.unlink()
        reconcile.run_sync(index=False)
        self.assertEqual(catalog.browse({})['total'],0)
        discovered=catalog.browse({'availability':'all'})
        self.assertEqual(discovered['total'],1)
        self.assertEqual(discovered['results'][0]['files'],[])


if __name__=='__main__':unittest.main()
