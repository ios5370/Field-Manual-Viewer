import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from field_manual_viewer import core, launcher, APP_VERSION
from field_manual_viewer.server_app import ThreadingHTTPServer, Handler


class LauncherTests(unittest.TestCase):
    def test_uses_system_browser_without_requiring_firefox(self):
        with patch.object(launcher.webbrowser,'open',return_value=True) as open_browser:
            self.assertTrue(launcher.browser('http://127.0.0.1:8765'))
            open_browser.assert_called_once_with('http://127.0.0.1:8765')

    def test_missing_default_browser_has_actionable_error(self):
        with patch.object(launcher.webbrowser,'open',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'Default apps'):
                launcher.browser('http://127.0.0.1:8765')

    def test_browser_failure_stops_new_server_and_releases_state_and_lock(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=core.ROOT/'tmp') as folder:
            root=Path(folder)
            with patch.object(core,'ROOT',root),patch.object(launcher,'ROOT',root),patch.object(launcher.webbrowser,'open',return_value=False),patch('sys.argv',['launcher','--port','0']):
                with self.assertRaisesRegex(RuntimeError,'default web browser'):
                    launcher.main()
                self.assertFalse((root/'data/server.json').exists())
                with core.FileLock('server'):pass

    def test_occupied_port_selects_another_exclusive_loopback_port(self):
        first=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        second=None
        try:
            second=launcher.bind_server(ThreadingHTTPServer,Handler,first.server_port)
            self.assertNotEqual(first.server_port,second.server_port)
            self.assertEqual(second.server_address[0],'127.0.0.1')
        finally:
            first.server_close()
            if second:second.server_close()

    def test_stop_refuses_copied_state_pointing_to_another_folder(self):
        with patch.object(launcher,'health',return_value={'root':str(core.ROOT/'another-library')}),patch.object(launcher.urllib.request,'build_opener') as network:
            with self.assertRaisesRegex(RuntimeError,'another library folder'):
                launcher.stop_server({'url':'http://127.0.0.1:8765','token':'copied'})
            network.assert_not_called()

    def test_saved_addresses_cannot_contact_external_hosts(self):
        for url in ('https://example.com','http://localhost:8765','http://127.0.0.1:8765@evil.test','http://127.0.0.1:8765/not-local'):
            with self.assertRaises(RuntimeError):launcher.local_url({'url':url})

    def test_missing_folder_identity_never_matches_the_current_library(self):
        self.assertFalse(launcher.same_library({}))
        self.assertFalse(launcher.same_library({'root':''}))
        self.assertFalse(launcher.same_library({'root':'.'}))

    def test_relaunch_reuses_only_this_folder_and_current_app_version(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=core.ROOT/'tmp') as folder:
            root=Path(folder)
            with patch.object(core,'ROOT',root),patch.object(launcher,'ROOT',root):
                core.save_json(root/'data/server.json',{'url':'http://127.0.0.1:8765','token':'this-folder'})
                with core.FileLock('server'),patch.object(launcher,'health',return_value={'root':str(root),'app':'Field Manual Viewer','version':APP_VERSION}),patch.object(launcher,'reserve_server',return_value={'reserved':True}),patch.object(launcher,'bind_server') as bind,patch('sys.argv',['launcher','--no-browser']):
                    launcher.main()
                    bind.assert_not_called()


if __name__=='__main__':unittest.main()
