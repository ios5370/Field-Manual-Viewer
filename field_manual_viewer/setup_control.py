"""Small process bridge for the Windows Forms setup window."""
import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from .core import ROOT, FileLock, load_json, save_json
from .lifecycle import inventory, locked, now

ACTIONS=('download','test','index','verify','repair','refresh')


def activity(phase, message, **extra):
    save_json(ROOT / 'reports/setup-job.json', {'pid': os.getpid(), 'state': 'running',
              'phase': phase, 'action':extra.pop('action',None) or load_json(ROOT/'reports/setup-job.json',{}).get('action'), 'message': message, 'updated_utc': now(), **extra})


def start_job(action, authorized=False):
    if action not in ACTIONS:raise ValueError('Choose a valid operation in Library Setup.')
    if action in ('download', 'repair', 'refresh') and not authorized:
        raise ValueError('Please confirm this download in Library Setup first.')
    with FileLock('setup-launch'):
        if any(locked(name) for name in ('setup-job', 'download-startup', 'acquisition', 'index')):
            return {'started': False, 'message': 'A library operation is already running. Reconnecting to its progress.'}
        (ROOT / 'data/pause-acquisition').unlink(missing_ok=True)
        (ROOT / 'reports').mkdir(exist_ok=True)
        (ROOT / 'tmp').mkdir(exist_ok=True)
        env = os.environ.copy()
        env.update(TEMP=str(ROOT / 'tmp'), TMP=str(ROOT / 'tmp'), PYTHONUNBUFFERED='1')
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        with (ROOT / 'reports/setup-worker.log').open('a', encoding='utf8') as log:
            child = subprocess.Popen([sys.executable, '-u', '-m', 'field_manual_viewer.setup_control', 'worker', action],
                  cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                  creationflags=flags, start_new_session=os.name != 'nt')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if locked('setup-job'):
                return {'started': True, 'pid': child.pid, 'message': 'Library preparation started.'}
            if child.poll() is not None:
                report = load_json(ROOT / 'reports/setup-job.json', {})
                if report.get('error'):
                    raise RuntimeError(report['error'])
                return {'started': True, 'pid': child.pid, 'message': report.get('message', 'Operation finished.')}
            time.sleep(.05)
        return {'started': True, 'pid': child.pid, 'message': 'Starting. Progress will appear shortly.'}


def worker(action):
    if action not in ACTIONS:raise ValueError('Choose a valid operation in Library Setup.')
    from .collect import CampaignPaused
    from .lifecycle import reconcile_hashes, finalize
    with FileLock('setup-job'):
        try:
            save_json(ROOT / 'data/setup-worker.json', {'pid':os.getpid(),'executable':str(sys.executable)})
            activity('verifying', 'Checking your saved originals.', action=action)
            previously_ready = load_json(ROOT / 'data/completion.json', {}).get('ready')
            issues = reconcile_hashes(lambda message: activity('verifying', message))
            if issues and previously_ready and action in ('download', 'test'):
                raise RuntimeError('Some originals need repair. Choose Advanced > Repair missing or failed originals to authorize retrieval; verified files are retained.')
            if action == 'verify':
                state = finalize()
                message = 'Integrity check finished. ' + (f'{len(state["issues"])} issues are listed in Advanced > Review errors.' if state['issues'] else 'Saved originals match their recorded checksums.')
            elif action == 'index':
                from .indexing import build
                activity('indexing', 'Preparing your documents for searching.')
                build()
                state = finalize()
                message = 'Indexing finished. ' + str(state['pending_indexes']) + ' indexes remain pending.'
            else:
                from .download import execute
                execute(test=action == 'test', refresh=action == 'refresh', retry_failed=action == 'repair', prepared=True)
                state = finalize()
                message = ('Your offline library is ready.' if not state['missing_files'] and not state['pending_indexes']
                           else 'Preparation finished. Review the counts and any exceptions below.')
            save_json(ROOT / 'reports/setup-job.json', {'state': 'finished', 'phase': 'finished',
                      'message': message, 'action':action, 'updated_utc': now(), 'pid': os.getpid()})
        except (CampaignPaused, KeyboardInterrupt) as error:
            save_json(ROOT / 'reports/setup-job.json', {'state': 'paused', 'phase': 'paused',
                      'message': 'Your previously downloaded files are safe.', 'action':action, 'error': str(error), 'updated_utc': now()})
        except Exception as error:
            traceback.print_exc()
            save_json(ROOT / 'reports/setup-job.json', {'state': 'failed', 'phase': 'failed',
                      'message': 'Preparation stopped. Your saved files are safe.', 'action':action, 'error': str(error), 'updated_utc': now()})
            raise


def _write_pause():
    (ROOT/'data').mkdir(exist_ok=True)
    (ROOT/'data/pause-acquisition').write_text('Pause requested\n')


def request_pause():
    # Serialize with launch so a just-starting job cannot erase a close request.
    with FileLock('setup-launch'):
        _write_pause()
    return {'message':'Pause requested. Current work will stop safely at a checkpoint.'}


def wait_worker_exit():
    """Wait for our recorded Windows worker's process, not just its lock release."""
    if os.name!='nt':return
    import ctypes
    from ctypes import wintypes
    info=load_json(ROOT/'data/setup-worker.json',{})
    pid=info.get('pid')
    expected=(ROOT/'runtime/python/python.exe').resolve()
    if not pid or pid==os.getpid() or os.path.normcase(os.path.realpath(info.get('executable','')))!=os.path.normcase(str(expected)):return
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x100000|0x1000,False,int(pid))
    if not handle:
        code=ctypes.get_last_error()
        if code==87:return  # Already exited.
        raise ctypes.WinError(code)
    try:
        if kernel.WaitForSingleObject(handle,0)==0:return
        size=wintypes.DWORD(32768);name=ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)):
            if kernel.WaitForSingleObject(handle,0)==0:return
            raise ctypes.WinError(ctypes.get_last_error())
        if os.path.normcase(os.path.realpath(name.value))!=os.path.normcase(str(expected)):return
        if kernel.WaitForSingleObject(handle,0xFFFFFFFF)!=0:raise ctypes.WinError(ctypes.get_last_error())
    finally:kernel.CloseHandle(handle)


def pause_and_wait():
    # Block new starts while closing; never terminate a process or delete work.
    guard=FileLock('setup-launch')
    while True:
        try:guard.__enter__();break
        except RuntimeError:time.sleep(.1)
    try:
        _write_pause()
        while any(locked(name) for name in ('setup-job','download-startup','acquisition','index')):
            time.sleep(.2)
        wait_worker_exit()
    finally:guard.__exit__(None,None,None)
    return {'closed_safely':True,'message':'Preparation paused. Saved files and search progress are retained.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('status', 'start', 'worker', 'pause', 'close'))
    parser.add_argument('action', nargs='?', choices=ACTIONS)
    parser.add_argument('--authorize', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.command in ('start','worker') and args.action is None:
        parser.error('Choose a Library Setup operation; no download has started.')
    for folder in ('data', 'reports', 'tmp'):
        (ROOT / folder).mkdir(exist_ok=True)
    try:
        if args.command == 'status':
            value = inventory()
        elif args.command == 'start':
            value = start_job(args.action, args.authorize)
        elif args.command == 'pause':
            value=request_pause()
        elif args.command == 'close':
            value=pause_and_wait()
        else:
            worker(args.action)
            return
    except Exception as error:
        value = {'error': str(error)}
    if args.output:
        path = (ROOT / args.output).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError('Status output must stay inside this library.')
        save_json(path, value)
    else:
        print(json.dumps(value))


if __name__ == '__main__':
    main()
