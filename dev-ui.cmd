@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev-ui.ps1" %*
exit /b %ERRORLEVEL%
