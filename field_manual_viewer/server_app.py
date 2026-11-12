"""Loopback-only offline application with range-aware original file serving."""
import html
import hashlib
import base64
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as BaseThreadingHTTPServer
from . import APP_VERSION
from .core import ROOT, database
from .search import search
from . import catalog, readers, reconcile
from .viewer_lifetime import ViewerLifetime

STATIC = ROOT / 'field_manual_viewer/static'


class ThreadingHTTPServer(BaseThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'

    def __init__(self,*args,**kwargs):
        self.viewer_lifetime=ViewerLifetime()
        self.lifetime_done=threading.Event()
        self.stop_token=secrets.token_hex(24)
        self.viewer_instance=secrets.token_hex(16)
        super().__init__(*args,**kwargs)

    def start_idle_shutdown(self):
        def watch():
            while not self.lifetime_done.wait(.5):
                if self.viewer_lifetime.begin_shutdown():
                    self.shutdown();return
        threading.Thread(target=watch,name='viewer-lifetime',daemon=True).start()

    def server_close(self):
        self.lifetime_done.set()
        super().server_close()

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        # pythonw.exe has no stderr; the default handler crashes on every response.
        if sys.stderr is not None and not getattr(sys.stderr,'closed',False):
            super().log_message(*args)

    def local_request(self):
        allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        if self.server.server_port==80:allowed.update(('127.0.0.1','localhost'))
        if self.headers.get('Host','').lower() not in allowed:
            self.send(403,'Open this library using its launcher and local address.','text/plain; charset=utf-8')
            return False
        return True

    def send(self, code, payload, mime='text/html; charset=utf-8', extra=None):
        payload = payload.encode('utf8') if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self' ws://127.0.0.1:"+str(self.server.server_port)+" ws://localhost:"+str(self.server.server_port)+"; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(payload)

    def json(self, value):
        self.send(200, json.dumps(value,ensure_ascii=False), 'application/json; charset=utf-8')

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        try:
            if not self.local_request():return
            self.get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        except FileNotFoundError as e: self.send(404,str(e),'text/plain; charset=utf-8')
        except (ValueError, KeyError) as e: self.send(400,str(e),'text/plain; charset=utf-8')
        except Exception as e:
            self.log_error('%s',e)
            self.send(500,'Field Manual Viewer could not complete this request. '+str(e),'text/plain; charset=utf-8')

    def get(self):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        for instance in (q.get('instance'),self.headers.get('X-Viewer-Instance')):
            if instance and instance!=self.server.viewer_instance:
                return self.send(409,'This viewer session has closed or changed. Reopen its library launcher.','text/plain; charset=utf-8')
        if path == '/' or path.startswith('/read/') or path.startswith('/original/pdf/'):
            page=(STATIC/'index.html').read_text(encoding='utf8').replace('__VIEWER_INSTANCE__',self.server.viewer_instance).replace('__APP_VERSION__',APP_VERSION)
            for asset in ('app.js','style.css'):
                revision=hashlib.sha256((STATIC/asset).read_bytes()).hexdigest()[:12]
                page=page.replace('/assets/'+asset,'/assets/'+asset+'?v='+revision)
            return self.send(200,page)
        if path == '/favicon.ico': return self.send(204,b'')
        if path == '/api/viewer-session': return self.viewer_session(q.get('instance'))
        if path.startswith('/assets/'):
            name=path[8:]
            if name not in ('style.css','app.js'): raise FileNotFoundError('Asset not found')
            return self.send(200,(STATIC/name).read_bytes(),'text/css' if name.endswith('.css') else 'text/javascript; charset=utf-8')
        if path=='/api/health': return self.json({'app':'Field Manual Viewer','root':str(ROOT),'version':APP_VERSION})
        if path=='/api/reconcile':
            if q.get('scan')=='force': reconcile.ensure_started(force=True)
            elif q.get('scan')=='1': reconcile.ensure_started()
            return self.json(reconcile.report())
        if path=='/api/status': return self.json(catalog.status())
        if path=='/api/catalog': return self.json(catalog.browse(q))
        if path=='/api/search': return self.json(search(q.pop('q',''),q,scope=q.pop('scope','all')))
        if path.startswith('/api/publication/'):
            p=catalog.details(path[17:])
            if not p: raise FileNotFoundError('Unknown publication')
            return self.json(p)
        m=re.fullmatch(r'/api/pdf-page/([^/]+)/(\d+)\.png',path)
        if m: return self.send(200,readers.pdf_image(m[1],int(m[2])),'image/png')
        m=re.fullmatch(r'/api/pdf-text/([^/]+)/(\d+)',path)
        if m:
            readers.archived(m[1],'pdf')
            db=database()
            lookup=db.execute("SELECT l.passage_rowid FROM pdf_pages l JOIN index_state i ON i.pub_id=l.pub_id AND i.format='pdf' JOIN files f ON f.pub_id=i.pub_id AND f.format=i.format WHERE l.pub_id=? AND l.page=? AND i.sha256=f.sha256 AND i.error IS NULL AND i.engine=?",(m[1],int(m[2]),catalog.ENGINE)).fetchone()
            row=db.execute('SELECT body FROM passages WHERE rowid=? AND pub_id=? AND page=?',(lookup[0],m[1],int(m[2]))).fetchone() if lookup else None
            total=db.execute('SELECT pages FROM extraction WHERE pub_id=?',(m[1],)).fetchone()
            db.close()
            if not total:
                # A verified original is readable before its searchable text is ready.
                import pypdfium2 as pdfium
                with readers.PDF_LOCK:
                    doc=pdfium.PdfDocument(readers.archived(m[1],'pdf'))
                    try: total=(len(doc),)
                    finally: doc.close()
            if int(m[2])<1 or int(m[2])>total[0]: raise ValueError('Page outside document')
            return self.json({'text':row[0] if row else '', 'total':total[0]})
        if path.startswith('/api/epub/'):
            return self.json(readers.epub_content(path[10:],q.get('locator')))
        if path.startswith('/files/'):
            file=(ROOT/path[7:]).resolve()
            if not file.is_relative_to((ROOT/'archive').resolve()) or file.suffix not in ('.pdf','.epub') or not file.is_file():
                raise FileNotFoundError('Not found')
            size,start,end=file.stat().st_size,0,file.stat().st_size-1
            ranged=self.headers.get('Range')
            if ranged:
                m=re.fullmatch(r'bytes=(\d*)-(\d*)',ranged)
                if not m or not any(m.groups()): return self.send(416,b'',extra={'Content-Range':f'bytes */{size}'})
                if m[1]: start=int(m[1]);end=int(m[2]) if m[2] else end
                else: start=max(0,size-int(m[2]))
                end=min(end,size-1)
                if start>end or start>=size: return self.send(416,b'',extra={'Content-Range':f'bytes */{size}'})
            with self.server.viewer_lifetime.connection():
                self.send_response(206 if ranged else 200)
                self.send_header('Content-Type','application/pdf' if file.suffix=='.pdf' else 'application/epub+zip')
                self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
                self.send_header('X-Content-Type-Options','nosniff')
                if ranged: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                if self.command!='HEAD':
                    with file.open('rb') as stream:
                        stream.seek(start);remaining=end-start+1
                        while remaining:
                            chunk=stream.read(min(256*1024,remaining))
                            if not chunk: break
                            self.wfile.write(chunk);remaining-=len(chunk)
            return
        if path.startswith('/details/'):
            p=catalog.details(path[9:])
            if not p: raise FileNotFoundError('Unknown publication')
            links=' '.join(f'<a href="/files/{html.escape(f["path"],quote=True)}">Open {f["format"].upper()}</a>' for f in p['files'] if f.get('available'))
            return self.send(200,'<meta charset="utf-8"><a href="/">Library</a><h1>'+html.escape(p.get('title') or p['id'])+'</h1>'+links+'<pre>'+html.escape(json.dumps(p,indent=2))+'</pre>')
        raise FileNotFoundError('Not found')

    def viewer_session(self,instance=None):
        origin=self.headers.get('Origin')
        if origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
            return self.send(403,'Unexpected viewer origin')
        if self.headers.get('Upgrade','').lower()!='websocket' or 'upgrade' not in {part.strip() for part in self.headers.get('Connection','').lower().split(',')} or self.headers.get('Sec-WebSocket-Version')!='13':
            return self.send(400,'Viewer session requires a WebSocket connection')
        key=self.headers.get('Sec-WebSocket-Key','')
        try:
            if len(key)>64 or len(base64.b64decode(key,validate=True))!=16:raise ValueError()
        except ValueError:return self.send(400,'Invalid viewer session key')
        if instance!=self.server.viewer_instance:return self.send(409,'Viewer session changed; reopen its launcher.')
        def frame(opcode,payload=b''):
            header=bytes([0x80|opcode,len(payload)]) if len(payload)<126 else bytes([0x80|opcode,126])+struct.pack('!H',len(payload))
            self.wfile.write(header+payload);self.wfile.flush()
        try:session=self.server.viewer_lifetime.join()
        except RuntimeError:return self.send(503,'Viewer is closing; reopen its launcher.')
        try:
            self.protocol_version='HTTP/1.1';self.close_connection=True
            self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade')
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode(),usedforsecurity=False).digest()).decode()
            self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
            frame(1,json.dumps({'type':'ready','close_token':self.server.stop_token}).encode())
            while True:
                header=self.rfile.read(2)
                if len(header)!=2:break
                first,second=header;opcode=first&15;length=second&127
                if first&0x70 or not first&0x80 or not second&0x80:break
                if length in (126,127):
                    raw=self.rfile.read(2 if length==126 else 8)
                    if len(raw)!=(2 if length==126 else 8):break
                    length=int.from_bytes(raw,'big')
                if length>4096 or (opcode>=8 and length>125):break
                mask=self.rfile.read(4);payload=self.rfile.read(length)
                if len(mask)!=4 or len(payload)!=length:break
                payload=bytes(value^mask[n%4] for n,value in enumerate(payload))
                if opcode==8:frame(8,payload);break
                if opcode==9:frame(10,payload)
                elif opcode not in (1,2,10):break
        except (OSError,ValueError):pass
        finally:self.server.viewer_lifetime.leave(session)

    def do_POST(self):
        try:
            if not self.local_request():return
            instance=self.headers.get('X-Viewer-Instance')
            if instance and instance!=self.server.viewer_instance:return self.send(409,'Viewer session changed')
            origin=self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
                return self.send(403,'Unexpected origin')
            if self.headers.get('Content-Type')!='application/json': return self.send(415,'JSON required')
            length=int(self.headers.get('Content-Length','0'))
            if length<0:return self.send(400,'Invalid request length')
            if length>4096: return self.send(413,'Request too large')
            self.connection.settimeout(10)
            data=json.loads(self.rfile.read(length))
            if self.path=='/api/viewer-launch' and data.get('token')==self.server.stop_token:
                if self.server.viewer_lifetime.reserve_launch():return self.json({'reserved':True})
                return self.send(503,'Viewer is closing')
            if self.path=='/api/stop' and data.get('token')==getattr(self.server,'stop_token',None) and data.get('token'):
                self.server.viewer_lifetime.request_shutdown()
                self.json({'stopping':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            self.send(404,'Not found')
        except Exception as e: self.send(400,str(e),'text/plain')


def serve(port=8765):
    print(f'Field Manual Viewer offline search: http://127.0.0.1:{port}',flush=True)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.start_idle_shutdown()
    try:server.serve_forever()
    finally:server.server_close()
