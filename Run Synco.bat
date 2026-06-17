@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 synco.py
) else (
    python synco.py
)

if errorlevel 1 (
    echo.
    echo Synco could not start.
    echo Check %%APPDATA%%\Synco\error.log for details.
    echo.
    pause
)
