@echo off
setlocal
cd /d "%~dp0"
if not exist "runtime\python\python.exe" (
 echo The portable runtime is missing. Extract the complete Field Manual Viewer ZIP first.
 pause
 exit /b 1
)
"runtime\python\python.exe" -m field_manual_viewer.status
echo.
pause
