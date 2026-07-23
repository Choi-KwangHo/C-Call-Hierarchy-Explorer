@echo off
if /I "%~1"=="--check" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" -CheckOnly
) else if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" -Version "%~1"
)
exit /b %ERRORLEVEL%
