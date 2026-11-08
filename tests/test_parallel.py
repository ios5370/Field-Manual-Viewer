import io
import threading
import time
import tempfile
import unittest
import urllib.error
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from field_manual_viewer.core import ROOT
from field_manual_viewer.collect import transfer,validate
from field_manual_viewer.parallel_network import SharedClient,HostGate,NetworkHalted,ServerBackoff,record_server_backoff,ensure_backoff_wait_elapsed
from field_manual_viewer.network import AccessDenied
from test_reliability import epub,Response


class Parallel(unittest.TestCase):
    def test_host_pacing_is_shared_across_threads(self):
        gate=HostGate({'host':.04})
        starts=[]
        lock=threading.Lock()
        def request(_):
            gate.wait('host')
            with lock:starts.append(time.monotonic())
        with ThreadPoolExecutor(4) as pool:list(pool.map(request,range(8)))
        starts.sort()
        self.assertTrue(all(b-a>=.035 for a,b in zip(starts,starts[1:])))
        self.assertEqual(gate.snapshot()['requests']['host'],8)

    def test_backoff_delays_every_worker_on_the_host(self):
        gate=HostGate({'host':.001})
        begin=time.monotonic();gate.defer('host',.12)
        def request(_):gate.wait('host');return time.monotonic()-begin
        with ThreadPoolExecutor(2) as pool:delays=list(pool.map(request,range(2)))
        self.assertGreaterEqual(min(delays),.10)
        self.assertEqual(gate.snapshot()['shared_backoffs']['host'],1)

    def test_429_pauses_all_workers_and_records_retry_after(self):
        client=SharedClient()
        response=Response(epub());response.url='https://files.trex-arms.com/test.epub'
        error=urllib.error.HTTPError(response.url,429,'slow down',{'Retry-After':'7'},io.BytesIO())
        with patch.object(client,'policy'),patch.object(client,'raw',side_effect=[error,response]) as raw,patch('field_manual_viewer.parallel_network.record_server_backoff') as record:
            with self.assertRaises(ServerBackoff):client.open(response.url)
            with self.assertRaises(NetworkHalted):client.open(response.url)
            record.assert_called_once_with(response.url,429,7)
            self.assertEqual(raw.call_count,1)
            self.assertTrue(client.stop.is_set())

    def test_503_and_backoff_from_robots_pause_and_alert(self):
        client=SharedClient();url='https://files.trex-arms.com/robots.txt'
        error=urllib.error.HTTPError(url,503,'busy',{},io.BytesIO())
        with patch.object(client,'raw',side_effect=error),patch('field_manual_viewer.parallel_network.record_server_backoff') as record:
            with self.assertRaises(ServerBackoff):client.policy('https://files.trex-arms.com/test.pdf')
            record.assert_called_once_with(url,503,60)
            self.assertTrue(client.stop.is_set())

    def test_server_backoff_sets_one_second_and_enforces_wait(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            root=Path(d);(root/'data').mkdir();(root/'reports').mkdir()
            (root/'data/acquisition-settings.json').write_text(json.dumps({'workers':4,'file_request_interval_seconds':.5}))
            with patch('field_manual_viewer.parallel_network.ROOT',root),patch('field_manual_viewer.parallel_network.subprocess.Popen') as notification:
                event=record_server_backoff('https://files.trex-arms.com/a.pdf',429,7)
                self.assertEqual(json.loads((root/'data/acquisition-settings.json').read_text())['file_request_interval_seconds'],1.0)
                self.assertEqual(json.loads((root/'reports/server-backoff.json').read_text())['status'],429)
                if __import__('os').name=='nt':notification.assert_called_once()
                now=datetime.datetime.fromisoformat(event['received_utc'])
                with self.assertRaises(RuntimeError):ensure_backoff_wait_elapsed(event,now)
                ensure_backoff_wait_elapsed(event,now+datetime.timedelta(seconds=8))

    def test_configured_half_second_pacing_and_robots_restriction(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            root=Path(d);(root/'data').mkdir()
            (root/'data/acquisition-settings.json').write_text(json.dumps({'file_request_interval_seconds':.5}))
            with patch('field_manual_viewer.parallel_network.ROOT',root):
                client=SharedClient()
                self.assertEqual(client.gate.snapshot()['minimum_start_intervals_seconds']['files.trex-arms.com'],.5)
                client.gate.restrict('files.trex-arms.com',1.25)
                self.assertEqual(client.gate.snapshot()['minimum_start_intervals_seconds']['files.trex-arms.com'],1.25)

    def test_access_denial_cancels_other_workers(self):
        client=SharedClient();url='https://files.trex-arms.com/no.pdf'
        error=urllib.error.HTTPError(url,403,'denied',{},io.BytesIO())
        with patch.object(client,'policy'),patch.object(client,'raw',side_effect=error) as raw:
            with self.assertRaises(AccessDenied):client.open(url)
            with self.assertRaises(NetworkHalted):client.open(url)
            self.assertEqual(raw.call_count,1)
        self.assertTrue(client.stop.is_set())

    def test_stream_cancellation_retains_partial_validator(self):
        client=SharedClient()
        class Stream(Response):
            def read(self,size=-1):
                data=super().read(size)
                if self.tell()>=256*1024:client.halt('test stop')
                return data
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            dest=Path(d)/'partial.epub'
            with patch.object(client,'open',return_value=Stream(epub())):
                with self.assertRaises(NetworkHalted):transfer(client,'url',dest,'epub')
            self.assertFalse(dest.exists())
            self.assertEqual(dest.with_suffix('.epub.part').stat().st_size,128*1024)
            self.assertTrue(dest.with_suffix('.epub.part.json').exists())

    def test_two_real_simultaneous_transfers_and_range_resume(self):
        self.simultaneous_transfers(2)

    def test_four_real_simultaneous_transfers_and_range_resume(self):
        self.simultaneous_transfers(4)

    def simultaneous_transfers(self,workers):
        body=epub();active=0;peak=0;lock=threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                nonlocal active,peak
                offset=int(self.headers.get('Range','bytes=0-')[6:-1])
                with lock:active+=1;peak=max(peak,active)
                try:
                    self.send_response(206 if offset else 200)
                    self.send_header('Content-Type','application/epub+zip')
                    self.send_header('Content-Length',str(len(body)-offset))
                    self.send_header('ETag','"test-v1"')
                    if offset:self.send_header('Content-Range',f'bytes {offset}-{len(body)-1}/{len(body)}')
                    self.end_headers();time.sleep(.1)
                    self.wfile.write(body[offset:])
                except (BrokenPipeError,ConnectionResetError):pass
                finally:
                    with lock:active-=1
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        client=SharedClient(gate=HostGate({'127.0.0.1':.01}))
        try:
            with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d,patch.object(client,'policy'):
                def task(i):
                    path=Path(d)/f'{i}.epub';url=f'http://127.0.0.1:{server.server_port}/{i}'
                    with self.assertRaises(InterruptedError):transfer(client,url,path,'epub',interrupt_after=1)
                    result=transfer(client,url,path,'epub')
                    self.assertEqual(result['resumed_from'],128*1024)
                    self.assertEqual(path.read_bytes(),body)
                    self.assertEqual(validate(path,'epub'),result['sha256'])
                    return result['sha256']
                with ThreadPoolExecutor(workers) as pool:hashes=list(pool.map(task,range(workers)))
                self.assertEqual(len(set(hashes)),1)
                self.assertEqual(peak,workers)
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
