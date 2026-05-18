#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

APP_NAME="VS-Codex Thread Tools"
MAIN_SCRIPT="vs_codex_thread_tools.py"
VENV_DIR=".venv_build"
LOG_FILE="build_output.log"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: build_app.command must be run on macOS."
    exit 1
fi

echo "Building ${APP_NAME} for macOS..."
echo "Build log will be written to: $(pwd)/${LOG_FILE}"
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 was not found. Install Python for macOS first."
    echo "The python.org installer is usually the simplest option because it includes Tkinter."
    exit 1
fi

python3 - <<'PY'
import sys
print("Using Python", sys.version)
if sys.version_info >= (3, 15):
    raise SystemExit("ERROR: PyInstaller may not support this Python version yet. Use Python 3.12, 3.13, or 3.14.")
try:
    import tkinter
except Exception as exc:
    raise SystemExit(f"ERROR: Tkinter is not available in this Python install: {exc}")
PY

echo "Creating isolated build environment..."
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv "${VENV_DIR}" >> "${LOG_FILE}" 2>&1
fi

# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"

echo "Installing/updating build dependencies..."
python -m pip install --upgrade pip setuptools wheel >> "${LOG_FILE}" 2>&1
python -m pip install --upgrade -r requirements-build.txt >> "${LOG_FILE}" 2>&1

echo "Cleaning previous build output..."
rm -rf build dist "${APP_NAME}.spec"

echo "Running PyInstaller..."
python -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "${APP_NAME}" \
    --hidden-import collections.abc \
    "${MAIN_SCRIPT}" >> "${LOG_FILE}" 2>&1

if [[ ! -d "dist/${APP_NAME}.app" ]]; then
    echo "ERROR: PyInstaller finished but dist/${APP_NAME}.app was not found."
    echo "Last lines from build log:"
    tail -30 "${LOG_FILE}" || true
    exit 1
fi

echo "Creating distributable zip..."
rm -f "dist/${APP_NAME}-macOS.zip"
if command -v ditto >/dev/null 2>&1; then
    (cd dist && ditto -c -k --sequesterRsrc --keepParent "${APP_NAME}.app" "${APP_NAME}-macOS.zip")
else
    (cd dist && zip -qry "${APP_NAME}-macOS.zip" "${APP_NAME}.app")
fi

echo
echo "Build complete."
echo "Your macOS app bundle is here:"
echo "$(pwd)/dist/${APP_NAME}.app"
echo
echo "A zip suitable for sharing is here:"
echo "$(pwd)/dist/${APP_NAME}-macOS.zip"
echo
echo "Full build log: $(pwd)/${LOG_FILE}"
echo "Warning details, if any: $(pwd)/build/${APP_NAME}/warn-${APP_NAME}.txt"
echo
