"""Keep successful setup hidden; surface even PowerShell parse/startup errors."""
import os
import subprocess
import sys
from .core import ROOT, save_json


def show_error(message):
    if os.name=='nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,message,'Field Manual Viewer - Library Setup',0x10)


def main():
    try:
        command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-STA','-File',str(ROOT/'Library-Setup.ps1'),*sys.argv[1:]]
        result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or 'PowerShell exited before setup completed.')[-8000:])
    except Exception as error:
        path=ROOT/'reports/setup-launcher-error.json'
        try:save_json(path,{'error':str(error),'root':str(ROOT)})
        except OSError:pass
        show_error('Library Setup could not start or stopped unexpectedly. Your saved manuals are retained.\n\nDetails: '+str(path)+'\n\n'+str(error)[:600])
        return 1
    return 0


if __name__=='__main__':sys.exit(main())
