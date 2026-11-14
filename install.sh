#!/usr/bin/env bash
# Fyrn installer: private .venv with PySide6 + llama-cpp-python,
# menu icon, and a fyrn launcher. Run with BASH (never sh).
# No terminal is needed to use Fyrn — double-click
# Install-Fyrn instead.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "Installing Fyrn..."

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is needed and was not found."
  exit 1
fi

# python3-venv missing is the common failure on a fresh Mint install:
# say exactly how to fix it instead of failing mid-venv with a
# confusing ensurepip error. This script never runs apt itself.
if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
  echo "python3-venv is missing."
  echo "Install it with this one command, then run install.sh again:"
  echo "  sudo apt install python3-venv"
  exit 1
fi

if [ ! -d .venv ]; then
  if ! python3 -m venv .venv; then
    echo "Creating the virtual environment failed."
    echo "On Mint this usually means python3-venv is missing:"
    echo "  sudo apt install python3-venv"
    echo "Then run install.sh again."
    exit 1
  fi
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# Fatal check: the packages must actually import before the
# installer reports success (a silent ModuleNotFoundError behind a
# menu icon is how installs die quietly). Import, never the
# distribution name: the installed distribution is
# PySide6-Essentials, not "PySide6".
if ! .venv/bin/python - <<'PYEOF'
import sys
missing = []
try:
    import PySide6  # noqa: F401
except ImportError:
    missing.append("PySide6")
try:
    import llama_cpp  # noqa: F401
except ImportError:
    missing.append("llama-cpp-python")
if missing:
    print("Install failed: missing packages: %s" % ", ".join(missing))
    sys.exit(1)
print("Packages present: PySide6, llama-cpp-python")
PYEOF
then
  exit 1
fi

# Warning only: can the GUI actually start on this machine's system
# Qt libraries? If not, name the failure — the install itself is
# still complete, and the message says what is missing.
if ! QT_QPA_PLATFORM=offscreen .venv/bin/python -c "
from PySide6.QtWidgets import QApplication
import sys
app = QApplication(sys.argv)
" 2>"$DIR/.gui-smoke-error.txt"; then
  echo "WARNING: the Fyrn window could not start here."
  echo "The install is complete, but the GUI failed its first start:"
  sed 's/^/  /' "$DIR/.gui-smoke-error.txt" | head -5
  echo "This usually means a system Qt library is missing."
  echo "Try: sudo apt install libegl1 libgl1"
fi
rm -f "$DIR/.gui-smoke-error.txt"

mkdir -p "$HOME/.local/share/applications" "$HOME/.local/bin"

sed "s|__APP_DIR__|$DIR|g" Fyrn.desktop \
  > "$HOME/.local/share/applications/Fyrn.desktop"

if [ -d "$HOME/Desktop" ]; then
  cp "$HOME/.local/share/applications/Fyrn.desktop" \
    "$HOME/Desktop/Fyrn.desktop" || true
  chmod +x "$HOME/Desktop/Fyrn.desktop" || true
  gio set "$HOME/Desktop/Fyrn.desktop" metadata::trusted true \
    2>/dev/null || true
fi

# PYTHONPATH points at the app folder: without it, `fyrn` only
# works when the terminal happens to be in the app folder, because
# `python -m fyrn` finds the package through the working directory.
{
  echo '#!/usr/bin/env bash'
  echo "PYTHONPATH=\"$DIR\${PYTHONPATH:+:\$PYTHONPATH}\" exec \"$DIR/.venv/bin/python\" -m fyrn \"\$@\""
} > "$HOME/.local/bin/fyrn"
chmod +x "$HOME/.local/bin/fyrn"

echo "Fyrn installed."
echo "Start it from the Fyrn icon, or run: fyrn"
echo "If 'fyrn' is not found, log out and back in once."

if [ "${FYRN_GUI_INSTALL:-}" = "1" ]; then
  read -rp "Done. Press Enter to close."
fi
