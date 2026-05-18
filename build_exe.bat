@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set APP_NAME=VS-Codex Thread Tools
set MAIN_SCRIPT=vs_codex_thread_tools.py
set VENV_DIR=.venv_build
set LOG_FILE=build_output.log

echo Building %APP_NAME%...
echo Build log will be written to: %CD%\%LOG_FILE%
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python launcher "py" was not found. Install Python for Windows first.
    echo Download from https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

py -3 -c "import sys; print('Using Python', sys.version); raise SystemExit(0 if sys.version_info < (3, 15) else 'ERROR: PyInstaller does not support Python 3.15 beta releases yet. Install Python 3.12, 3.13, or 3.14.')"
if errorlevel 1 goto :error

echo Creating isolated build environment...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    py -3 -m venv "%VENV_DIR%" >> "%LOG_FILE%" 2>&1
    if errorlevel 1 goto :error
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto :error

echo Installing/updating build dependencies...
python -m pip install --upgrade pip setuptools wheel >> "%LOG_FILE%" 2>&1
if errorlevel 1 goto :error

python -m pip install --upgrade -r requirements-build.txt >> "%LOG_FILE%" 2>&1
if errorlevel 1 goto :error

echo Cleaning previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

echo Running PyInstaller...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "%APP_NAME%" ^
    --hidden-import collections.abc ^
    "%MAIN_SCRIPT%" >> "%LOG_FILE%" 2>&1
if errorlevel 1 goto :error

if not exist "dist\%APP_NAME%.exe" (
    echo ERROR: PyInstaller finished but the exe was not found.
    goto :error
)

echo.
echo Build complete.
echo Your executable is here:
echo %CD%\dist\%APP_NAME%.exe
echo.
echo Runtime crash log, if created:
echo %%LOCALAPPDATA%%\VS-Codex Thread Tools\crash.log
echo.
echo Full build log: %CD%\%LOG_FILE%
echo Warning details, if any: %CD%\build\%APP_NAME%\warn-%APP_NAME%.txt
echo.
pause
exit /b 0

:error
echo.
echo Build failed. Open this log and copy the last 30 lines if you want me to inspect it:
echo %CD%\%LOG_FILE%
echo.
if exist "%LOG_FILE%" (
    echo Last lines from build log:
    powershell -NoProfile -Command "Get-Content -Tail 30 '%LOG_FILE%'"
)
echo.
pause
exit /b 1
