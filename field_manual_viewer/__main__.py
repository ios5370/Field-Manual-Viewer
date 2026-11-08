import argparse
import json
from .core import ROOT


def main():
    parser = argparse.ArgumentParser(description='Field Manual Viewer portable archive and offline search')
    sub = parser.add_subparsers(dest='command', required=True)
    d=sub.add_parser('discover');d.add_argument('--refresh',action='store_true')
    sub.add_parser('sample')
    c = sub.add_parser('collect')
    c.add_argument('--id', action='append')
    c.add_argument('--format', choices=['pdf', 'epub', 'both'], default='both')
    c.add_argument('--branch')
    c.add_argument('--type', dest='pub_type')
    c.add_argument('--category', dest='functional_category')
    c.add_argument('--collection')
    sub.add_parser('index')
    s = sub.add_parser('search')
    s.add_argument('query')
    s.add_argument('--scope', default='all', choices=['all', 'body', 'title', 'toc', 'designation'])
    web = sub.add_parser('serve')
    web.add_argument('--port', type=int, default=8765)
    sub.add_parser('report')
    b=sub.add_parser('bulk');b.add_argument('--limit',type=int);b.add_argument('--retry-failed',action='store_true');b.add_argument('--workers',type=int,choices=(1,2,3,4))
    sub.add_parser('pause')
    args = parser.parse_args()
    if args.command == 'discover':
        from .discover import discover
        discover(args.refresh)
    elif args.command == 'sample':
        from .collect import select_sample
        print(select_sample())
    elif args.command == 'collect':
        from .collect import collect
        ids = args.id
        filters = {k: getattr(args, k) for k in ('branch', 'pub_type', 'functional_category', 'collection') if getattr(args, k)}
        if filters:
            sample = json.loads((ROOT / 'data/sample.json').read_text())
            records = json.loads((ROOT / 'data/manifest.json').read_text(encoding='utf8'))
            ids = [p['id'] for p in records if p['id'] in (ids or sample) and all(p.get(k) == v for k,v in filters.items())]
        collect(ids, ('pdf', 'epub') if args.format == 'both' else (args.format,))
    elif args.command == 'index':
        from .search import build_index
        build_index()
    elif args.command == 'search':
        from .search import search
        print(json.dumps(search(args.query, scope=args.scope), indent=2, ensure_ascii=False))
    elif args.command == 'serve':
        from .server import serve
        serve(args.port)
    elif args.command == 'report':
        from .report import report
        print(json.dumps(report(), indent=2))
    elif args.command == 'bulk':
        from .campaign import run
        print(json.dumps(run(args.limit,args.retry_failed,args.workers),indent=2))
    elif args.command == 'pause':
        (ROOT/'data/pause-acquisition').write_text('Pause requested by user\n')
        print('Acquisition will stop safely at its next checkpoint.')


if __name__ == '__main__':
    main()
