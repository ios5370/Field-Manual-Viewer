"""Open this library's viewer, isolating different folders and old versions."""
import argparse
import errno
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from . import APP_VERSION
from .core import ROOT, FileLock, save_json, load_json


def browser(url,bundled=False):
    exe=next((ROOT/'runtime/browsers').glob('chromium-*/chrome-win64/chrome.exe'),None)
    if bundled and exe:
        return subprocess.Popen([str(exe),'--user-data-dir='+str(ROOT/'runtime/browser-profile'),
            '--no-first-run','--no-default-browser-check','--disable-gpu','--disable-background-networking',
            '--disable-component-update','--disable-sync',url],cwd=ROOT)
    message='Windows could not open a default web browser. Choose a web browser in Settings > Apps > Default apps, then launch the viewer again. Firefox is not required.'
    try:opened=webbrowser.open(url)
    except (OSError,webbrowser.Error) as error:raise RuntimeError(message) from error
    if not opened:raise RuntimeError(message)
    return True


def open_browser_for_server(server,url,bundled=False):
    try:browser(url,bundled)
    except Exception as error:
        server.browser_launch_error=str(error)
        server.viewer_lifetime.request_shutdown()
        server.shutdown()


def local_url(info):
    url=info.get('url','')
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise RuntimeError('Saved viewer address is not a local library endpoint.')
    return url.rstrip('/')


def health(info):
    url=local_url(info)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url+'/api/health',timeout=3) as response:
        return json.load(response)


def same_library(identity):
    folder=identity.get('root')
    if not isinstance(folder,str) or not folder or not os.path.isabs(folder):return False
    return os.path.normcase(os.path.realpath(folder)) == os.path.normcase(str(ROOT.resolve()))


def stop_server(info):
    identity=health(info)
    if not same_library(identity):
        raise RuntimeError('That viewer belongs to another library folder and was left running.')
    token=info.get('token')
    if not token:raise RuntimeError('The running viewer has no saved stop token.')
    request=urllib.request.Request(local_url(info)+'/api/stop',data=json.dumps({'token':token}).encode(),
                                   headers={'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=3) as response:
        return json.load(response)


def reserve_server(info):
    request=urllib.request.Request(local_url(info)+'/api/viewer-launch',
        data=json.dumps({'token':info.get('token')}).encode(),headers={'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=3) as response:return json.load(response)


def bind_server(server_type,handler,preferred):
    try:
        return server_type(('127.0.0.1',preferred),handler)
    except OSError as error:
        if error.errno not in (errno.EADDRINUSE,10048,10013) and getattr(error,'winerror',None) not in (10048,10013):
            raise
        # Another library or local service owns the preferred port. Keep it intact.
        return server_type(('127.0.0.1',0),handler)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundled-browser',action='store_true')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--stop',action='store_true')
    parser.add_argument('--port',type=int,default=8765)
    opts=parser.parse_args();state=ROOT/'data/server.json'
    if opts.stop:
        info=load_json(state,{})
        if info:print(stop_server(info))
        return
    from .server_app import Handler,ThreadingHTTPServer
    for module in ('pypdfium2','PIL','json5','bs4','cryptography'):__import__(module)
    from .core import database
    # Any in-place prototype upgrade is local and completes before serving pages.
    db=database(); db.close()
    from .indexing import ensure_pdf_lookup
    ensure_pdf_lookup()
    from . import catalog
    deadline=time.monotonic()+8
    upgrade_requested=False
    prewarm_thread=None
    while True:
        guard=FileLock('server')
        try:
            guard.__enter__()
        except RuntimeError:
            if upgrade_requested:
                if time.monotonic()<deadline:time.sleep(.1);continue
                raise RuntimeError('The earlier viewer is still closing. Please try again shortly.')
            info=load_json(state,{})
            # Another start can hold the lock just before publishing its address.
            if not info:
                if time.monotonic()<deadline:time.sleep(.1);continue
                raise RuntimeError('The viewer is starting; please try its launcher again shortly.')
            try:identity=health(info)
            except (OSError,ValueError):
                if time.monotonic()<deadline:time.sleep(.1);continue
                raise RuntimeError('A viewer process holds this folder but did not answer. See reports/launcher-error.json; a hidden older viewer may still be running.')
            if not same_library(identity):
                raise RuntimeError('Saved viewer information belongs to another library folder. It was not opened or stopped.')
            if identity.get('app')=='Field Manual Viewer' and identity.get('version')==APP_VERSION:
                try:reserve_server(info)
                except urllib.error.HTTPError as error:
                    if error.code==503 and time.monotonic()<deadline:time.sleep(.1);continue
                    raise
                except OSError:
                    if time.monotonic()<deadline:time.sleep(.1);continue
                    raise
                if not opts.no_browser:browser(local_url(info),opts.bundled_browser)
                print('Opened this library at '+local_url(info),flush=True)
                return
            # Upgrade only this folder's old viewer. Acquisition is independent.
            stop_server(info)
            upgrade_requested=True
            if time.monotonic()>=deadline:
                raise RuntimeError('The earlier viewer is still closing. Please try again shortly.')
            time.sleep(.2)
            continue
        try:
            server=bind_server(ThreadingHTTPServer,Handler,opts.port)
            server.stop_token=secrets.token_hex(24)
            url='http://127.0.0.1:'+str(server.server_port)
            save_json(state,{'url':url,'token':server.stop_token,'pid':os.getpid(),
                             'root':str(ROOT),'app':'Field Manual Viewer','version':APP_VERSION})
            print('Field Manual Viewer running at '+url,flush=True)
            prewarm_thread=threading.Thread(target=catalog.prewarm, name='catalog-prewarm', daemon=True)
            prewarm_thread.start()
            if not opts.no_browser:
                opener=threading.Timer(.5,open_browser_for_server,args=(server,url,opts.bundled_browser))
                opener.daemon=True
                opener.start()
            server.start_idle_shutdown()
            try:server.serve_forever()
            finally:
                server.server_close()
                try:state.unlink(missing_ok=True)
                except OSError:pass  # A copy operation can temporarily hold this disposable file.
            if getattr(server,'browser_launch_error',None):raise RuntimeError(server.browser_launch_error)
            return
        finally:
            if prewarm_thread and prewarm_thread.is_alive():
                prewarm_thread.join(2)
            guard.__exit__(None,None,None)


if __name__=='__main__':
    try:main()
    except Exception as error:
        message='Field Manual Viewer could not start: '+str(error)
        try:save_json(ROOT/'reports/launcher-error.json',{'error':message,'root':str(ROOT),'version':APP_VERSION})
        except OSError:pass  # Still show the error when this folder is not writable.
        if sys.stderr:print(message,file=sys.stderr)
        if os.name=='nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,message,'Field Manual Viewer',0x10)
        sys.exit(1)
