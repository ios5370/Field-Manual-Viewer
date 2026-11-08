"""Shared host pacing, backoff and cancellation for a bounded download pool."""
import collections
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import datetime
import os
import subprocess
import sys
from email.utils import parsedate_to_datetime
from .network import Client,AccessDenied,NoRedirect,USER_AGENT
from .core import ROOT,load_json,save_json


class NetworkHalted(RuntimeError):
    pass


class ServerBackoff(AccessDenied):
    pass


def ensure_backoff_wait_elapsed(event=None,now=None):
    event=event if event is not None else load_json(ROOT/'reports/server-backoff.json',{})
    if not event.get('resume_not_before_utc'):return
    now=now or datetime.datetime.now(datetime.timezone.utc)
    until=datetime.datetime.fromisoformat(event['resume_not_before_utc'])
    if now<until:
        raise RuntimeError('Server requested a wait before resuming; retry after '+until.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'))


def record_server_backoff(url,status,seconds):
    """Persist the requested wait and set the next run to the one-second fallback."""
    now=datetime.datetime.now(datetime.timezone.utc)
    host=urllib.parse.urlsplit(url).hostname
    event={'status':status,'host':host,'url':url,'received_utc':now.isoformat(),
           'retry_after_seconds':seconds,
           'resume_not_before_utc':(now+datetime.timedelta(seconds=seconds)).isoformat(),
           'action':'Campaign paused; next resume uses a 1.0-second file request interval.'}
    settings=load_json(ROOT/'data/acquisition-settings.json',{})
    settings['file_request_interval_seconds']=1.0
    settings['fallback_reason']='Server requested backoff; 1.0-second file pacing on next resume.'
    save_json(ROOT/'data/acquisition-settings.json',settings)
    save_json(ROOT/'reports/server-backoff.json',event)
    with (ROOT/'reports/server-backoff-history.jsonl').open('a',encoding='utf8') as log:
        import json
        log.write(json.dumps(event)+'\n')
    if os.name=='nt':
        executable=ROOT/'runtime/python/pythonw.exe'
        try:
            subprocess.Popen([str(executable),'-m','field_manual_viewer.notifier',str(status),host,str(seconds)],
                             cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as e:
            event['notification_error']=str(e)
            save_json(ROOT/'reports/server-backoff.json',event)
    return event


class HostGate:
    def __init__(self, intervals=None, stop=None):
        self.intervals=intervals or {'www.trex-arms.com':1.5,'files.trex-arms.com':1.0}
        self.stop=stop or threading.Event()
        self.condition=threading.Condition()
        self.next_start={}
        self.cooldown={}
        self.requests=collections.Counter()
        self.backoffs=collections.Counter()

    def wait(self,host):
        with self.condition:
            while True:
                if self.stop.is_set(): raise NetworkHalted('Shared acquisition stop requested')
                now=time.monotonic()
                delay=max(self.next_start.get(host,0),self.cooldown.get(host,0))-now
                if delay<=0:
                    self.next_start[host]=now+self.intervals.get(host,1.5)
                    self.requests[host]+=1
                    return
                self.condition.wait(min(delay,.2))

    def defer(self,host,seconds):
        with self.condition:
            self.cooldown[host]=max(self.cooldown.get(host,0),time.monotonic()+seconds)
            self.backoffs[host]+=1
            self.condition.notify_all()

    def restrict(self,host,seconds):
        with self.condition:
            self.intervals[host]=max(self.intervals.get(host,0),seconds)

    def snapshot(self):
        with self.condition:
            return {'requests':dict(self.requests),'shared_backoffs':dict(self.backoffs),
                    'minimum_start_intervals_seconds':dict(self.intervals)}


class SharedClient(Client):
    def __init__(self,stop=None,gate=None):
        super().__init__()
        self.stop=stop or threading.Event()
        settings=load_json(ROOT/'data/acquisition-settings.json',{})
        file_interval=settings.get('file_request_interval_seconds',1.0)
        if file_interval not in (0.5,1.0):raise ValueError('File request interval must be 0.5 or 1.0 seconds')
        self.gate=gate or HostGate({'www.trex-arms.com':1.5,'files.trex-arms.com':file_interval},stop=self.stop)
        self.policy_lock=threading.RLock()
        self.stop_reason=None
        self.http_errors=collections.Counter()
        self.error_lock=threading.Lock()
        self.throttle_lock=threading.Lock()
        self.throttle_recorded=False

    def check_stopped(self):
        if self.stop.is_set(): raise NetworkHalted(self.stop_reason or 'Shared acquisition stop requested')

    def halt(self,reason):
        with self.error_lock:
            if not self.stop_reason:self.stop_reason=str(reason)
        self.stop.set()
        with self.gate.condition:self.gate.condition.notify_all()

    def raw(self,url,headers=None):
        self.check_stopped()
        host=urllib.parse.urlsplit(url).hostname
        self.gate.wait(host)
        return urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url,headers={
            'User-Agent':USER_AGENT,'Accept-Encoding':'identity',**(headers or {})}),timeout=60)

    def policy(self,url):
        # Serialize first fetches of robots; raw requests have their own host gate.
        with self.policy_lock:
            try:
                u=urllib.parse.urlsplit(url)
                if u.scheme!='https' or u.hostname not in ('www.trex-arms.com','files.trex-arms.com') or u.netloc!=u.hostname:
                    raise AccessDenied('URL outside permitted HTTPS source hosts')
                host=u.hostname
                origin='https://'+host
                if origin not in self.policies:
                    try:
                        with self.raw(origin+'/robots.txt') as response:text=response.read().decode('utf8')
                    except urllib.error.HTTPError as error:
                        if error.code!=404:
                            with self.error_lock:self.http_errors[error.code]+=1
                        self.handle_backoff(error,origin+'/robots.txt')
                        if error.code!=404:raise AccessDenied(f'robots unavailable: {error.code}') from error
                        text='User-agent: *\nAllow: /\n'
                    parser=urllib.robotparser.RobotFileParser();parser.parse(text.splitlines())
                    self.policies[origin]=parser
                    save_json(ROOT/'data/discovery'/(host+'-policy.json'),{'url':origin+'/robots.txt','text':text,'checked':time.time()})
                parser=self.policies[origin]
                if not parser.can_fetch(USER_AGENT,url):raise AccessDenied('robots disallows '+url)
                delay=parser.crawl_delay(USER_AGENT)
                rate=parser.request_rate(USER_AGENT)
                if delay:self.gate.restrict(host,delay)
                if rate:self.gate.restrict(host,rate.seconds/rate.requests)
            except AccessDenied as e:
                self.halt(e)
                raise

    def handle_backoff(self,error,url):
        delay=error.headers.get('Retry-After','')
        if error.code not in (429,503) and not delay:return
        try:seconds=int(delay) if delay.isdigit() else max(0,parsedate_to_datetime(delay).timestamp()-time.time())
        except (ValueError,TypeError):seconds=0
        if not seconds:seconds=60
        reason=f'Server requested backoff (HTTP {error.code}); paused. Resume at 1.0-second file pacing after {int(seconds)} seconds.'
        self.halt(reason)
        with self.throttle_lock:
            if not self.throttle_recorded:
                record_server_backoff(url,error.code,seconds)
                self.throttle_recorded=True
        raise ServerBackoff(reason) from error

    def open(self,url,headers=None):
        self.check_stopped()
        try:
            self.policy(url)
            for attempt in range(3):
                try:
                    self.check_stopped()
                    response=self.raw(url,headers)
                    try:self.policy(response.url)
                    except Exception:response.close();raise
                    return response
                except urllib.error.HTTPError as e:
                    with self.error_lock:self.http_errors[e.code]+=1
                    self.handle_backoff(e,url)
                    if e.code in (401,403):
                        raise AccessDenied(f'Access denied ({e.code}): {url}') from e
                    if e.code not in (429,500,502,503,504):raise
                    delay=e.headers.get('Retry-After','')
                    try:seconds=int(delay) if delay.isdigit() else max(0,parsedate_to_datetime(delay).timestamp()-time.time())
                    except (ValueError,TypeError):seconds=0
                    if seconds>900:raise AccessDenied('Server requested a long retry delay; resume later') from e
                    self.gate.defer(urllib.parse.urlsplit(url).hostname,max(seconds,5*2**attempt))
                    if attempt==2:raise
        except AccessDenied as e:
            self.halt(e)
            raise

    def metrics(self):
        with self.error_lock:
            errors=dict(self.http_errors)
        return {**self.gate.snapshot(),'http_errors':errors,'stop_reason':self.stop_reason}
