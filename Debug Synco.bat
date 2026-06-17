@echo on
setlocal
cd /d "%~dp0"
echo Starting Synco from:
cd
echo.
where py
where python
echo.
py -3 "%~dp0synco.py"
echo.
echo Synco closed or failed.
echo Check %APPDATA%\Synco\error.log if there was an error.
pause
