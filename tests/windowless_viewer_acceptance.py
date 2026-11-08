"""Developer-only acceptance: requires a closed test library and Playwright."""
import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def run(root,browser_executable):
    root=root.resolve();state_file=root/'data/server.json'
    (root/'tmp').mkdir(exist_ok=True)
    (root/'reports').mkdir(exist_ok=True)
    os.environ.update(TEMP=str(root/'tmp'),TMP=str(root/'tmp'))
    if state_file.exists():raise RuntimeError('Stop this test library viewer before acceptance.')
    pythonw=root/'runtime/python/pythonw.exe'
    processes=[];checks={}
    def start(force_no_console=True):
        # Exercise the actual windowless image, including its absent console handles.
        code='import sys; sys.stdout=None; sys.stderr=None; from field_manual_viewer.launcher import main; main()'
        entry=['-c',code] if force_no_console else ['-m','field_manual_viewer.launcher']
        process=subprocess.Popen([str(pythonw),*entry,'--no-browser','--port','0'],cwd=root,
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes.append(process)
        for _ in range(150):
            if process.poll() is not None:raise RuntimeError('Windowless viewer exited before it answered.')
            try:
                state=json.loads(state_file.read_text())
                with urllib.request.urlopen(state['url']+'/api/health',timeout=1) as response:identity=json.load(response)
                if identity.get('root')==str(root):return process,state['url']
            except (OSError,ValueError,KeyError):pass
            time.sleep(.1)
        raise RuntimeError('Windowless viewer never answered health requests.')
    def stopped(process):
        process.wait(timeout=15)
        assert process.returncode==0,process.returncode
        assert not state_file.exists()
    def ready(page,url):
        page.goto(url);expect(page.locator('#viewer-close')).to_be_enabled(timeout=15000)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=str(browser_executable),headless=True,args=['--disable-gpu'])
            context=browser.new_context()
            process,url=start();checks['real_pythonw_answers_without_stderr']=True
            page=context.new_page();ready(page,url)
            # More than six tabs also checks that lifetime tracking cannot exhaust
            # the browser's ordinary HTTP connection pool.
            others=[]
            for _ in range(7):
                tab=context.new_page();ready(tab,url);others.append(tab)
            for tab in others:tab.close()
            time.sleep(6)
            assert process.poll() is None
            checks['one_remaining_background_tab_keeps_server_alive']=True
            for _ in range(3):
                page.reload();expect(page.locator('#viewer-close')).to_be_enabled()
            assert process.poll() is None;checks['reload_does_not_stop_server']=True
            expect(page.locator('#count')).to_contain_text('publication')
            # The native-original wrapper must keep the server alive too.
            if page.locator('a[href*="/read/pdf/"]').count():
                with context.expect_page() as opened:page.locator('a[href*="/read/pdf/"]').first.click()
                reader=opened.value;expect(reader.locator('#viewer-close')).to_be_enabled()
                with context.expect_page() as opened:reader.get_by_text('Open original PDF',exact=True).click()
                native=opened.value;expect(native.locator('#viewer-close')).to_be_enabled()
                expect(native.locator('iframe')).to_have_count(1)
                reader.close();page.close();time.sleep(6)
                assert process.poll() is None;native.close()
                checks['native_pdf_wrapper_keeps_server_alive']=True
            else:page.close()
            stopped(process);checks['closing_last_tab_stops_windowless_process']=True
            process,url=start();page=context.new_page();ready(page,url)
            browser.close();stopped(process)
            checks['closing_whole_browser_stops_windowless_process']=True
            browser=p.chromium.launch(executable_path=str(browser_executable),headless=True,args=['--disable-gpu'])
            context=browser.new_context();process,url=start(force_no_console=False);page=context.new_page();ready(page,url)
            second=context.new_page();ready(second,url)
            page.locator('#viewer-close').click()
            expect(page.locator('h1')).to_have_text('Viewer closed.')
            stopped(process);checks['close_viewer_button_releases_process_immediately']=True
            checks['actual_pythonw_module_entry_point']=True
            browser.close()
        result={'passed':True,'checks':checks}
        (root/'reports/windowless-viewer-acceptance.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result,indent=2))
        return result
    finally:
        for process in processes:
            if process.poll() is None:
                # This test started the exact PID and only its viewer process.
                process.terminate();process.wait(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--library-root',type=Path,required=True)
    parser.add_argument('--browser-executable',type=Path,required=True)
    args=parser.parse_args();run(args.library_root,args.browser_executable)
