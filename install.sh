#!/usr/bin/env bash
# Fyrn installer: private .venv with PySide6 + llama-cpp-python,
# menu icon, and a fyrn launcher. Run with BASH (never sh).
# No terminal is needed to use Fyrn — double-click
# Install-Fyrn instead.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "Installing Fyrn..."

# Disk space, before anything is created: the venv plus
# dependencies need room, and a mid-install ENOSPC leaves a
# broken half-install. 1 GB is the floor. If free space cannot
# be measured, warn and carry on: an unmeasurable check must
# not block an install.
# The || true keeps a failing df from killing the script
# under set -euo pipefail: an unmeasurable check warns and
# carries on, it never aborts the install.
FREE_KB="$(df -P -k "$DIR" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
case "$FREE_KB" in
  ''|*[!0-9]*)
    echo "Could not check free disk space; continuing anyway."
    ;;
  *)
    if [ "$FREE_KB" -lt 1048576 ]; then
      echo "Not enough free disk space to install Fyrn."
      echo "Fyrn needs at least 1 GB free where it is installed."
      exit 1
    fi
    ;;
esac

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

# The model engine wheel ships inside this zip (v0.7):
# llama-cpp-python 0.3.37 has no pre-built wheel on any index,
# so the release carries the wheel built from the 0.3.37 source.
# Check it is here before creating anything: a missing wheel
# fails here in plain words, not halfway through the install.
WHEEL="wheels/llama_cpp_python-0.3.37-py3-none-linux_x86_64.whl"
if [ ! -f "$WHEEL" ]; then
  echo "The model engine wheel is missing from this install."
  echo "Expected: $WHEEL"
  echo "Download Fyrn v0.7 again from the releases page and try again."
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
# Install the wheel first: requirements.txt then finds the pin
# already satisfied and never tries a source build (no compiler,
# no 15-30 minute compile on this PC). A wheel pip rejects is
# damaged, and fails here in plain words, not as a traceback.
if ! .venv/bin/pip install --quiet "$WHEEL"; then
  echo "The model engine could not be installed."
  echo "The wheel file is $WHEEL - it may be damaged."
  echo "Download Fyrn v0.7 again from the releases page and try again."
  exit 1
fi
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

# The desktop file starts Fyrn through the launcher in
# ~/.local/bin/fyrn (not run.sh directly): the launcher finds the
# folder again if it moved, so the icon keeps working after a
# move. Desktop files do not expand $HOME, so the home folder is
# baked in too.
sed -e "s|__APP_DIR__|$DIR|g" -e "s|__HOME__|$HOME|g" Fyrn.desktop \
  > "$HOME/.local/share/applications/Fyrn.desktop"

if [ -d "$HOME/Desktop" ]; then
  cp "$HOME/.local/share/applications/Fyrn.desktop" \
    "$HOME/Desktop/Fyrn.desktop" || true
  chmod +x "$HOME/Desktop/Fyrn.desktop" || true
  gio set "$HOME/Desktop/Fyrn.desktop" metadata::trusted true \
    2>/dev/null || true
fi

# The launcher, from the launcher.sh template: it starts Fyrn
# from this folder, and finds the folder again if it moved.
sed "s|__APP_DIR__|$DIR|g" launcher.sh > "$HOME/.local/bin/fyrn"
chmod +x "$HOME/.local/bin/fyrn"

echo "Fyrn installed."
echo "Start it from the Fyrn icon, or run: fyrn"
echo "If 'fyrn' is not found, log out and back in once."

if [ "${FYRN_GUI_INSTALL:-}" = "1" ]; then
  read -rp "Done. Press Enter to close."
fi
