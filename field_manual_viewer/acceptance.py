"""Real-library acceptance with all non-loopback Python sockets denied."""
import json
import re
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from pypdf import PdfReader
from .core import ROOT, database, digest, save_json
from .search import search
from .server import Handler


def run():
    original_connect = socket.socket.connect
    blocked = []

    def offline_connect(sock, address):
        if address[0] not in ('127.0.0.1', '::1', 'localhost'):
            blocked.append(str(address))
            raise OSError('Field Manual Viewer acceptance: external network disabled')
        return original_connect(sock, address)

    results = {'network_policy': 'All Python non-loopback socket connects disabled for entire acceptance run',
               'passages': [], 'epub_passages': [], 'checks': {}}
    with patch.object(socket.socket, 'connect', offline_connect):
        try:
            socket.create_connection(('1.1.1.1', 443), timeout=1)
            raise AssertionError('Network disable failed')
        except OSError:
            results['checks']['external_network_denied'] = True
        with database() as db:
            sample = json.loads((ROOT / 'data/sample.json').read_text())
            for pub_id in sample:
                # Interior body passage, not front matter or a publication title.
                row = db.execute('''SELECT page,body FROM passages WHERE pub_id=?
                    AND CAST(page AS INTEGER)>(SELECT min(15,max(CAST(page AS INTEGER))/4) FROM passages WHERE pub_id=?) AND length(body)>500
                    ORDER BY abs(CAST(page AS INTEGER)-(SELECT max(CAST(page AS INTEGER))/2 FROM passages WHERE pub_id=?)) LIMIT 1''',
                    (pub_id, pub_id, pub_id)).fetchone()
                assert row, f'No interior passage for {pub_id}'
                words = re.findall(r'[A-Za-z]{3,}', row['body'])
                # Choose a consecutive literal phrase from the original body, preserving punctuation.
                match = re.search(r'\b[A-Za-z]{3,}(?:[ \t\r\n]+[A-Za-z]{2,}){5}\b', row['body'])
                assert match, f'No phrase for {pub_id}'
                phrase = ' '.join(match[0].split())
                matches = search('"' + phrase + '"', {'id': pub_id}, scope='body')
                assert any(int(r['page']) == int(row['page']) for r in matches), pub_id
                f = db.execute("SELECT * FROM files WHERE pub_id=? AND format='pdf'", (pub_id,)).fetchone()
                original = PdfReader(ROOT / f['path']).pages[int(row['page']) - 1].extract_text() or ''
                assert phrase.lower() in ' '.join(original.split()).lower()
                assert digest(ROOT / f['path']) == f['sha256']
                results['passages'].append({'id': pub_id, 'physical_page': row['page'], 'phrase': phrase,
                                             'body_search_found': True, 'original_pdf_confirmed': True})
                print('offline body search verified:', pub_id, 'page', row['page'], flush=True)
                sections=db.execute('SELECT locator,body FROM epub_passages WHERE pub_id=? AND length(body)>1000 ORDER BY rowid',(pub_id,)).fetchall()
                assert sections, f'No EPUB body text for {pub_id}'
                section=sections[len(sections)//2]
                match=re.search(r'\b[A-Za-z]{3,}(?:[ \t\r\n]+[A-Za-z]{2,}){5}\b',section['body'])
                assert match, f'No EPUB phrase for {pub_id}'
                epub_phrase=' '.join(match[0].split())
                from .catalog import browse
                found=browse({'q':'"'+epub_phrase+'"','scope':'body','format':'epub','size':'100'})
                assert any(p['id']==pub_id for p in found['results']), f'EPUB search failed for {pub_id}'
                results['epub_passages'].append({'id':pub_id,'locator':section['locator'],'phrase':epub_phrase,'body_search_found':True})
        rows = search('"human will"', scope='body')
        results['checks']['phrase_search'] = bool(rows)
        rows = search('MCDP 1', {'branch': 'usmc'}, scope='designation')
        assert rows and all(r['branch'] == 'usmc' for r in rows)
        results['checks']['identifier_and_branch_filter'] = True
        assert not search('MCDP 1', {'branch': 'navy'}, scope='designation')
        results['checks']['negative_filter'] = True
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with urllib.request.urlopen(base) as r:
                page = r.read().decode()
                assert 'FIELD MANUAL' in page and 'https://' not in page
            results['checks']['offline_ui_http'] = True
            with urllib.request.urlopen(base + '/api/search?q=human+will&scope=body') as r:
                rows = json.load(r)
                assert rows
            with urllib.request.urlopen(base + '/details/' + rows[0]['pub_id']) as r:
                assert b'Open PDF' in r.read()
            with urllib.request.urlopen(base + rows[0]['local_url'].split('#')[0]) as r:
                assert r.read(5) == b'%PDF-'
            results['checks']['details_and_local_pdf'] = True
            try:
                urllib.request.urlopen(base + '/files/../data/manifest.json')
                raise AssertionError('Path traversal allowed')
            except urllib.error.HTTPError as e:
                assert e.code == 404
            results['checks']['file_path_boundary'] = True
        finally:
            server.shutdown()
            server.server_close()
    results['blocked_external_attempts'] = blocked
    results['passed'] = len(results['passages']) == 20 and len(results['epub_passages']) == 20 and all(results['checks'].values())
    save_json(ROOT / 'reports/offline-acceptance.json', results)
    print(json.dumps(results['checks'], indent=2))
    return results


if __name__ == '__main__':
    run()
