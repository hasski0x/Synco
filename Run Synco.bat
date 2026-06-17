@echo off
setlocal
cd /d "%~dp0"

if exist "%LocalAppData%\Programs\Python\Python312\pythonw.exe" (
    start "Synco" "%LocalAppData%\Programs\Python\Python312\pythonw.exe" "%~dp0synco.py"
    exit /b 0
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "Synco" pyw "%~dp0synco.py"
    exit /b 0
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "Synco" py -3 "%~dp0synco.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "Synco" python "%~dp0synco.py"
    exit /b 0
)

echo Python was not found.
echo Install Python 3, then run Synco again.
pause
