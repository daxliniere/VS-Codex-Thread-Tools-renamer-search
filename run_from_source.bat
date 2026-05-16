@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo VS-Codex Thread Tools v17 - run from source
echo Folder: %CD%
echo.
echo This window should stay open while the GUI is running.
echo Runtime log: %%LOCALAPPDATA%%\VS-Codex Thread Tools\runtime.log
echo Crash log:  %%LOCALAPPDATA%%\VS-Codex Thread Tools\crash.log
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python launcher "py" was not found.
    echo Install Python for Windows from https://www.python.org/downloads/windows/
    echo Make sure "py launcher" is enabled during install.
    echo.
    pause
    exit /b 1
)

echo Python detected:
py -3 -c "import sys; print(sys.version)"
if errorlevel 1 (
    echo.
    echo ERROR: Could not run Python with py -3.
    echo.
    pause
    exit /b 1
)

echo.
echo Checking Tkinter availability...
py -3 -c "import tkinter; print('Tkinter OK')"
if errorlevel 1 (
    echo.
    echo ERROR: Tkinter is not available in this Python installation.
    echo Install standard Python from python.org, then try again.
    echo.
    pause
    exit /b 1
)

echo.
echo Launching GUI...
echo.
set PYTHONUNBUFFERED=1
py -3 -u vs_codex_thread_tools.py
set EXITCODE=%ERRORLEVEL%

echo.
echo GUI process exited with code %EXITCODE%.
if not "%EXITCODE%"=="0" (
    echo.
    echo The app exited with an error.
    echo Runtime log: %%LOCALAPPDATA%%\VS-Codex Thread Tools\runtime.log
    echo Crash log:  %%LOCALAPPDATA%%\VS-Codex Thread Tools\crash.log
)
echo.
pause
exit /b %EXITCODE%
