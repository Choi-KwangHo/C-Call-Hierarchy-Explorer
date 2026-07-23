@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" -Version "%~1"
exit /b %ERRORLEVEL%
