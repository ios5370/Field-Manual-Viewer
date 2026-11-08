"""Acceptance against a real 20-edition pilot; requires developer Playwright."""
import argparse
import json
import re
import sqlite3
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def run(root, executable, port):
    base=f'http://127.0.0.1:{port}'
    errors, external, checks=[],[],{}
    db_path=root/'data/field-manual-viewer.sqlite'
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=str(executable),headless=True,args=['--disable-gpu'])
        ctx=browser.new_context(viewport={'width':1440,'height':1000})
        def route(request):
            if request.request.url.startswith(base+'/'):request.continue_()
            else:external.append(request.request.url);request.abort()
        ctx.route(re.compile(r'^https?://'),route)
        page=ctx.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        expect.set_options(timeout=30000)
        page.goto(base+'/?availability=archived&q=Warfighting&scope=title')
        page.wait_for_selector('article[data-id="mcdp_1_undated"]')
        assert page.locator('#stats,#campaign,#foot-status').count()==0
        expect(page.locator('footer')).to_contain_text('Not affiliated with or endorsed by T.REX ARMS.')
        expect(page.locator('footer a')).to_have_attribute('href','https://www.trex-arms.com/field-manual-vault')
        checks['publisher_credit_and_explicit_online_link']=True
        checks['accepted_layout_without_acquisition_dashboard']=True
        # Mimic a newly verified ledger entry, then refresh the same server.
        with sqlite3.connect(db_path) as db:
            prior=list(db.execute('SELECT format,status FROM files WHERE pub_id=?',('mcdp_1_undated',)))
            db.execute("UPDATE files SET status='interrupted' WHERE pub_id=?",('mcdp_1_undated',))
        try:
            page.reload();expect(page.locator('article[data-id="mcdp_1_undated"]')).to_have_count(0)
        finally:
            with sqlite3.connect(db_path) as db:
                for fmt,status in prior:db.execute('UPDATE files SET status=? WHERE pub_id=? AND format=?',(status,'mcdp_1_undated',fmt))
        page.reload();page.wait_for_selector('article[data-id="mcdp_1_undated"]')
        checks['refresh_reveals_verified_files_without_server_restart']=True
        # A manually removed format must disappear even while its index remains.
        with sqlite3.connect(db_path) as db:
            rel=db.execute("SELECT path FROM files WHERE pub_id=? AND format='pdf'",('mcdp_1_undated',)).fetchone()[0]
        original=(root/rel).resolve()
        assert original.is_relative_to((root/'archive').resolve())
        saved=root/'tmp/availability-test-original.pdf'
        saved.parent.mkdir(exist_ok=True);assert not saved.exists()
        original.rename(saved)
        try:
            page.reload();page.wait_for_selector('article[data-id="mcdp_1_undated"]')
            card=page.locator('article[data-id="mcdp_1_undated"]')
            assert card.locator('.card-actions a[href*="/read/pdf/"]').count()==0
            assert card.locator('.card-actions a[href*="/read/epub/"]').count()==1
            page.goto(base+'/?availability=archived&q=%22human+will%22&scope=body&format=pdf')
            expect(page.locator('#count')).to_contain_text('publication')
            assert page.locator('article[data-id="mcdp_1_undated"]').count()==0
        finally:
            saved.rename(original)
        checks['removed_pdf_hides_button_and_stale_text_hits']=True
        page.goto(base+'/?availability=archived&q=%22human+will%22&scope=body&format=pdf&branch=usmc&year_from=1990&year_to=2020&sort=oldest')
        page.wait_for_selector('article[data-id="mcdp_1_undated"] .hit')
        page.reload();page.wait_for_selector('article[data-id="mcdp_1_undated"] .hit')
        assert page.locator('#branch').input_value()=='usmc'
        assert page.locator('#year_from').input_value()=='1990'
        assert page.locator('#year_to').input_value()=='2020'
        years=[int(y) for y in page.locator('article.card .eyebrow span:last-child').all_text_contents()]
        assert years and all(1990<=year<=2020 for year in years) and years==sorted(years)
        checks['filter_date_sort_and_refresh_preservation']=True
        card=page.locator('article[data-id="mcdp_1_undated"]')
        assert card.locator('mark').count()>0
        with ctx.expect_page() as tab:card.locator('.hit a').first.click()
        pdf=tab.value;pdf.wait_for_selector('.reader-page')
        expect(pdf.locator('.reader-page')).to_have_js_property('complete',True)
        assert pdf.locator('.reader-page').evaluate('e=>e.naturalWidth')>0
        number=int(pdf.locator('#page-number').input_value());assert number>10
        pdf.locator('#reader-next').click();expect(pdf.locator('#page-number')).to_have_value(str(number+1))
        checks['deep_pdf_search_rendering_and_page_navigation']=True
        card.locator('[data-details]').first.click();page.wait_for_selector('dialog[open]')
        page.locator('[data-related="mcdp_1"]').click();expect(page.locator('#detail-content')).to_contain_text('1997')
        page.locator('#close-details').click();checks['historical_edition_navigation']=True
        page.locator('#format').select_option('epub');page.wait_for_selector('.hit a[href*="/read/epub/"]')
        with ctx.expect_page() as tab:page.locator('.hit a[href*="/read/epub/"]').first.click()
        epub=tab.value;epub.wait_for_selector('.epub-text');assert len(epub.locator('.epub-text').inner_text())>100
        if not epub.locator('#reader-next').is_disabled():
            before=epub.locator('#chapter').input_value();epub.locator('#reader-next').click()
            expect(epub.locator('#chapter')).not_to_have_value(before)
        checks['epub_search_reading_and_chapter_navigation']=True
        page.goto(base+'/?scope=designation&q=MCDP+1&availability=archived')
        page.wait_for_selector('article.card');assert page.locator('article[data-id="mcdp_1"]').count()==1
        checks['publication_number_search']=True
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        checks['mobile_layout']=True
        assert not errors and not external,(errors,external)
        checks['no_external_requests_or_javascript_errors']=True
        page.screenshot(path=str(root/'reports/offline-viewer.png'),full_page=True)
        browser.close()
    result={'passed':True,'checks':checks,'javascript_errors':errors,'external_requests':external}
    (root/'reports/portable-browser-acceptance.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--library-root',type=Path,required=True)
    parser.add_argument('--browser-executable',type=Path,required=True);parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args();run(args.library_root,args.browser_executable,args.port)
