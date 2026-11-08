import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from .core import ROOT, save_json, save_text

USER_AGENT = 'Field Manual Viewer-Archive-Validation/0.1 (personal preservation; paced)'


class AccessDenied(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccessDenied('Redirect requires explicit source review: ' + newurl)


class Client:
    def __init__(self, pace=1.5, refresh=False):
        self.pace, self.last, self.policies, self.refresh = pace, 0, {}, refresh

    def raw(self, url, headers=None):
        time.sleep(max(0, self.pace - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        return urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity', **(headers or {})}), timeout=60)

    def policy(self, url):
        u = urllib.parse.urlsplit(url)
        if u.scheme != 'https' or u.hostname not in ('www.trex-arms.com', 'files.trex-arms.com'):
            raise AccessDenied('URL outside permitted HTTPS source hosts')
        origin = f'{u.scheme}://{u.netloc}'
        if origin not in self.policies:
            try:
                with self.raw(origin + '/robots.txt') as r:
                    text = r.read().decode('utf8')
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise AccessDenied(f'robots unavailable: {e.code}') from e
                text = 'User-agent: *\nAllow: /\n'
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(text.splitlines())
            self.policies[origin] = parser
            save_json(ROOT / 'data/discovery' / (u.hostname + '-policy.json'),
                      {'url': origin + '/robots.txt', 'text': text, 'checked': time.time()})
        if not self.policies[origin].can_fetch(USER_AGENT, url):
            raise AccessDenied('robots disallows ' + url)

    def open(self, url, headers=None):
        self.policy(url)
        for attempt in range(3):
            try:
                r = self.raw(url, headers)
                # Never follow an unexpected redirect into disallowed source paths.
                self.policy(r.url)
                return r
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise AccessDenied(f'Access denied ({e.code}): {url}') from e
                if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                delay = e.headers.get('Retry-After', '')
                # Honor numeric or HTTP-date Retry-After; do not shorten server requests.
                from email.utils import parsedate_to_datetime
                try: seconds = int(delay) if delay.isdigit() else max(0,parsedate_to_datetime(delay).timestamp()-time.time())
                except (ValueError,TypeError): seconds=0
                if seconds>900: raise RuntimeError('Server requested a long retry delay; resume later') from e
                time.sleep(max(5 * 2 ** attempt, seconds))

    def html(self, url):
        from .source import metadata
        key=hashlib.sha256(url.encode()).hexdigest()[:24]
        path=ROOT/'data/discovery'/(key+'.html')
        if path.exists() and not self.refresh:
            try:
                text=path.read_text(encoding='utf8')
                metadata(text)
                return text
            except (UnicodeError,ValueError):pass
        with self.open(url) as response:text=response.read().decode('utf8')
        metadata(text)
        if path.exists():save_text(path.with_name(path.stem+'-'+str(time.time_ns())+'.html'),path.read_text(encoding='utf8',errors='replace'))
        save_text(path,text)
        save_json(path.with_suffix('.json'),{'url':url,'fetched':time.time()})
        return text
