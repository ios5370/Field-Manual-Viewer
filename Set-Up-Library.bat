@echo off
cd /d "%~dp0"
if not exist "runtime\python\pythonw.exe" (
 echo Download the portable Windows ZIP from Releases. It includes the runtime.
 pause
 exit /b 1
)
start "" "runtime\python\pythonw.exe" -m field_manual_viewer.setup_launcher %*
