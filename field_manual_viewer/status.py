"""Human-readable library and search-index status."""
from .core import ROOT, load_json
from .lifecycle import inventory


def main():
    state = inventory()
    total = state.get('discovered') or state.get('catalog_expected') or 0
    print('Field Manual Viewer - library status')
    print('=' * 38)
    print(f'Publications:       {total:,} / {state.get("catalog_expected", total):,}')
    print(f'PDF searchable:    {state.get("pdf_indexed", 0):,} / {state.get("pdf_verified", 0):,} verified')
    print(f'EPUB searchable:   {state.get("epub_indexed", 0):,} / {state.get("epub_verified", 0):,} verified')
    print(f'Markdown searchable:{state.get("md_indexed", 0):,} / {state.get("md_verified", 0):,} verified')
    print(f'Pending indexes:   {state.get("pending_indexes", 0):,}')
    print(f'Indexing now:      {"yes" if state.get("active") else "no"}')
    progress = load_json(ROOT / 'reports/indexing-progress.json', {})
    if state.get('active') and progress.get('message'):
        print(f'Progress:          {progress["message"]}')
    print(f'Library state:     {state.get("state")} - {state.get("message")}')
    if state.get('pending_indexes', 0) == 0 and state.get('state') in ('complete', 'exceptions'):
        print('RESULT: search index is complete.')
        return 0
    if state.get('active'):
        print('RESULT: indexing is still running. Shutting down now is safe;')
        print('        reopen the app and run this check again, then resume.')
    else:
        print('RESULT: indexing is incomplete. Resume with:')
        print('        runtime\\python\\python.exe -m field_manual_viewer index')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
