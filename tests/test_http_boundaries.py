import http.client
import tempfile
import threading
import unittest
from unittest.mock import Mock
from pathlib import Path
from unittest.mock import patch
from field_manual_viewer import core, server_app


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=core.ROOT/'tmp');self.root=Path(self.temp.name)
        (self.root/'archive').mkdir()
        (self.root/'archive/example.pdf').write_bytes(b'%PDF-local-test')
        (self.root/'private.pdf').write_bytes(b'outside archive')
        self.patcher=patch.object(server_app,'ROOT',self.root);self.patcher.start()
        class QuietHandler(server_app.Handler):
            def log_message(self,*args):pass
        self.server=server_app.ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        self.server.stop_token='known-local-token'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        self.patcher.stop();self.temp.cleanup()

    def request(self,path,method='GET',headers=None,body=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            connection.request(method,path,body=body,headers=headers or {})
            response=connection.getresponse();return response.status,response.read(),dict(response.getheaders())
        finally:connection.close()

    def test_only_local_hostnames_can_read_the_viewer(self):
        status,_,_=self.request('/api/health',headers={'Host':'attacker.invalid'})
        self.assertEqual(status,403)
        self.assertEqual(self.request('/api/health')[0],200)

    def test_cross_origin_stop_is_rejected_even_with_a_token(self):
        status,_,_=self.request('/api/stop','POST',{'Origin':'https://attacker.invalid','Content-Type':'application/json'},b'{"token":"known-local-token"}')
        self.assertEqual(status,403)
        self.assertEqual(self.request('/api/health')[0],200)

    def test_invalid_and_excessive_body_lengths_are_rejected(self):
        for length,expected in (('-1',400),('5000',413)):
            status,_,_=self.request('/api/stop','POST',{'Content-Type':'application/json','Content-Length':length},b'')
            self.assertEqual(status,expected)

    def test_original_serving_cannot_escape_archive(self):
        for path in ('/files/archive/../private.pdf','/files/archive/%2e%2e/private.pdf','/files/archive/%2e%2e%5cprivate.pdf'):
            self.assertEqual(self.request(path)[0],404)
        status,data,headers=self.request('/files/archive/example.pdf',headers={'Range':'bytes=0-4'})
        self.assertEqual(status,206);self.assertEqual(data,b'%PDF-')

    def test_viewer_exposes_no_external_reader_or_acquisition_endpoint(self):
        for endpoint in ('/api/open-pdfcraft','/api/download','/api/acquire','/api/update'):
            status,_,_=self.request(endpoint,'POST',{'Content-Type':'application/json'},b'{"id":"example"}')
            self.assertEqual(status,404)

    def test_viewer_session_rejects_foreign_origin_and_bad_handshake(self):
        headers={'Origin':'https://attacker.invalid','Upgrade':'websocket','Connection':'Upgrade',
                 'Sec-WebSocket-Version':'13','Sec-WebSocket-Key':'a'*24}
        self.assertEqual(self.request('/api/viewer-session',headers=headers)[0],403)
        headers['Origin']=f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.request('/api/viewer-session',headers=headers)[0],400)
        self.assertEqual(len(self.server.viewer_lifetime.active),0)

    def test_disconnected_websocket_handshake_leaves_no_phantom_tab(self):
        handler=object.__new__(server_app.Handler);handler.server=self.server
        handler.headers={'Origin':f'http://127.0.0.1:{self.server.server_port}','Upgrade':'websocket',
                         'Connection':'keep-alive, Upgrade','Sec-WebSocket-Version':'13',
                         'Sec-WebSocket-Key':'dGhlIHNhbXBsZSBub25jZQ=='}
        handler.send_response=Mock(side_effect=BrokenPipeError())
        handler.viewer_session(self.server.viewer_instance)
        self.assertEqual(len(self.server.viewer_lifetime.active),0)

    def test_stale_page_cannot_use_a_different_server_instance(self):
        self.assertEqual(self.request('/api/health',headers={'X-Viewer-Instance':'old-copy'})[0],409)
        self.assertEqual(self.request('/files/archive/example.pdf?instance=old-copy')[0],409)
        self.assertEqual(self.request('/api/stop','POST',{'Content-Type':'application/json','X-Viewer-Instance':'old-copy'},b'{"token":"known-local-token"}')[0],409)
        self.assertEqual(self.request('/api/health')[0],200)


if __name__=='__main__':unittest.main()
