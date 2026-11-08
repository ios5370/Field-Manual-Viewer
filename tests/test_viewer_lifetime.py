import ctypes
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from field_manual_viewer import core
from field_manual_viewer.viewer_lifetime import ViewerLifetime
from field_manual_viewer.server_app import Handler


class ViewerLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.time=0
        self.life=ViewerLifetime(idle_seconds=5,startup_seconds=90,clock=lambda:self.time)

    def test_live_background_tab_never_expires_without_user_activity(self):
        tab=self.life.join();self.time=100000
        self.assertFalse(self.life.should_stop())
        self.life.leave(tab);self.time+=5
        self.assertTrue(self.life.should_stop())

    def test_closing_one_of_two_tabs_keeps_the_viewer_alive(self):
        first=self.life.join();second=self.life.join();self.life.leave(first)
        self.time=100000;self.assertFalse(self.life.should_stop())
        self.life.leave(second);self.time+=5;self.assertTrue(self.life.should_stop())

    def test_reload_reconnects_within_grace_period(self):
        tab=self.life.join();self.life.leave(tab);self.time=4
        replacement=self.life.join();self.time=20
        self.assertFalse(self.life.should_stop())
        self.life.leave(replacement);self.time=25;self.assertTrue(self.life.should_stop())

    def test_failed_browser_launch_does_not_leave_a_server_forever(self):
        self.time=89;self.assertFalse(self.life.should_stop())
        self.time=90;self.assertTrue(self.life.should_stop())

    def test_relaunch_reserves_time_for_the_new_browser_tab(self):
        tab=self.life.join();self.life.leave(tab);self.time=4
        self.assertTrue(self.life.reserve_launch());self.time=10
        self.assertFalse(self.life.should_stop())
        replacement=self.life.join();self.life.leave(replacement);self.time=15
        self.assertTrue(self.life.should_stop())

    def test_shutdown_cannot_accept_a_new_viewer_or_launch_reservation(self):
        self.time=90;self.assertTrue(self.life.begin_shutdown())
        self.assertFalse(self.life.reserve_launch())
        with self.assertRaises(RuntimeError):self.life.join()

    def test_transfer_keeps_server_alive_until_completion(self):
        with self.life.connection():
            self.time=1000;self.assertFalse(self.life.should_stop())
        self.time+=5;self.assertTrue(self.life.should_stop())

    def test_windowless_request_logging_does_not_raise(self):
        handler=object.__new__(Handler)
        with patch('sys.stderr',None):handler.log_message('status %s',200)

    @unittest.skipUnless(os.name=='nt','Windows kernel mutex')
    def test_server_lock_creates_no_copy_blocking_file(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=core.ROOT/'tmp') as folder,patch.object(core,'ROOT',Path(folder)):
            with core.FileLock('server'):
                self.assertFalse((Path(folder)/'data/server.lock').exists())
                with self.assertRaises(RuntimeError):
                    with core.FileLock('server'):pass

    @unittest.skipUnless(os.name=='nt','Legacy Windows byte lock')
    def test_old_server_byte_lock_is_still_recognized(self):
        import msvcrt
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=core.ROOT/'tmp') as folder,patch.object(core,'ROOT',Path(folder)):
            path=Path(folder)/'data/server.lock';path.parent.mkdir();path.write_bytes(b'0')
            with path.open('r+b') as stream:
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                try:
                    with self.assertRaises(RuntimeError):
                        with core.FileLock('server'):pass
                finally:stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            with core.FileLock('server'):
                self.assertEqual(path.read_bytes(),b'0')


if __name__=='__main__':unittest.main()
