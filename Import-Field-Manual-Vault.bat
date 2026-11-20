@echo off
setlocal
cd /d "%~dp0"
if not exist "runtime\python\python.exe" (
 echo The portable runtime is missing. Extract the complete Field Manual Viewer ZIP first.
 pause
 exit /b 1
)
set "SOURCE=%~1"
if "%SOURCE%"=="" (
 if exist "D:\Field Manual Vault\T.Rex Arms Field Manual Vault\index.csv" set "SOURCE=D:\Field Manual Vault\T.Rex Arms Field Manual Vault"
 if "%SOURCE%"=="" if exist "D:\Field Manual Vault\index.csv" set "SOURCE=D:\Field Manual Vault"
)
if "%SOURCE%"=="" (
 echo Drag the extracted "T.Rex Arms Field Manual Vault" folder onto this file,
 echo or run: Import-Field-Manual-Vault.bat "D:\path\to\vault"
 pause
 exit /b 1
)
echo Importing from: %SOURCE%
echo This can take a long time. Existing verified files are skipped.
"runtime\python\python.exe" -m field_manual_viewer.import_vault --source "%SOURCE%"
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
 echo.
 echo Import stopped with exit code %CODE%. See reports\import-vault.json.
 pause
 exit /b %CODE%
)
echo.
echo Import and search preparation finished.
pause
