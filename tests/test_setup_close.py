import tempfile
import threading
import time
import unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from field_manual_viewer import core, setup_control
from field_manual_viewer.collect import CampaignPaused


class SetupCloseTests(unittest.TestCase):
    def setUp(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=core.ROOT/'tmp');self.root=Path(self.temp.name)
        self.stack=ExitStack()
        for module in (core,setup_control):self.stack.enter_context(patch.object(module,'ROOT',self.root))
    def tearDown(self):self.stack.close();self.temp.cleanup()

    def test_close_waits_for_startup_then_checkpoint_and_does_not_spawn_work(self):
        done=threading.Event();errors=[];result=[];release=threading.Event()
        def worker():
            try:
                with core.FileLock('setup-job'),core.FileLock('index'):
                    release.set()
                    while not (self.root/'data/pause-acquisition').exists():time.sleep(.01)
                    time.sleep(.15)
                    (self.root/'data/test-checkpoint').write_text('retained')
            except Exception as error:errors.append(error)
        def close():
            try:result.append(setup_control.pause_and_wait())
            except Exception as error:errors.append(error)
            finally:done.set()
        job=threading.Thread(target=worker);closer=threading.Thread(target=close)
        with patch.object(setup_control,'wait_worker_exit') as wait_exit,patch.object(setup_control.subprocess,'Popen') as spawn:
            with core.FileLock('setup-launch'):
                job.start();self.assertTrue(release.wait(2));closer.start()
                self.assertFalse(done.wait(.1))
                self.assertFalse((self.root/'data/pause-acquisition').exists())
            self.assertTrue(done.wait(5));job.join(2);closer.join(2)
            self.assertEqual(errors,[]);self.assertTrue(result[0]['closed_safely'])
            self.assertEqual((self.root/'data/test-checkpoint').read_text(),'retained')
            wait_exit.assert_called_once();spawn.assert_not_called()

    def test_just_started_worker_does_not_erase_a_close_pause(self):
        (self.root/'data').mkdir()
        (self.root/'data/pause-acquisition').write_text('requested')
        with patch('field_manual_viewer.lifecycle.reconcile_hashes',side_effect=CampaignPaused('paused')):
            setup_control.worker('index')
        self.assertTrue((self.root/'data/pause-acquisition').exists())
        self.assertEqual(core.load_json(self.root/'reports/setup-job.json')['state'],'paused')

    def test_explicit_new_start_clears_previous_pause_before_spawning(self):
        (self.root/'data').mkdir()
        (self.root/'data/pause-acquisition').write_text('old pause')
        def spawn(*args,**kwargs):
            self.assertFalse((self.root/'data/pause-acquisition').exists())
            raise RuntimeError('No child created by this test')
        with patch.object(setup_control.subprocess,'Popen',side_effect=spawn):
            with self.assertRaisesRegex(RuntimeError,'No child'):setup_control.start_job('index')

    @unittest.skipUnless(__import__('os').name=='nt','Windows sharing semantics')
    def test_progress_save_waits_for_windows_reader_and_retains_atomic_json(self):
        import ctypes
        from ctypes import wintypes
        path=self.root/'progress.json';core.save_json(path,{'old':True})
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.CreateFileW(str(path),0x80000000,3,None,3,0,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        done=threading.Event();errors=[]
        def writer():
            try:core.save_json(path,{'new':True})
            except Exception as error:errors.append(error)
            finally:done.set()
        thread=threading.Thread(target=writer)
        try:
            thread.start();self.assertFalse(done.wait(.1))
            self.assertEqual(core.load_json(path),{'old':True})
        finally:kernel.CloseHandle(handle)
        self.assertTrue(done.wait(3));thread.join();self.assertEqual(errors,[])
        self.assertEqual(core.load_json(path),{'new':True})
        self.assertEqual(list(self.root.glob('*.tmp')),[])
