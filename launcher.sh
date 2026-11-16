#!/usr/bin/env bash
# Fyrn launcher: installed to ~/.local/bin/fyrn by install.sh,
# which fills in __APP_DIR__ with the folder Fyrn was installed
# from. It starts Fyrn from that folder — and if the folder moved
# or was renamed, it looks for the new home before giving up, so
# the desktop icon keeps working after a move.
set -euo pipefail
APP_DIR="__APP_DIR__"

# The install is still where it was: start Fyrn.
if [ -f "$APP_DIR/fyrn/__init__.py" ] \
    && [ -x "$APP_DIR/.venv/bin/python" ]; then
  # PYTHONPATH points at the app folder: without it, `fyrn` only
  # works when the terminal happens to be in the app folder,
  # because `python -m fyrn` finds the package through the
  # working directory.
  PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    exec "$APP_DIR/.venv/bin/python" -m fyrn "$@"
fi

# The folder moved. Look for it: a Fyrn install is a folder with
# install.sh, the fyrn package, and a venv. Hidden folders are
# skipped (the search stays in the user's own files).
CANDIDATES=()
while IFS= read -r -d '' init; do
  dir="$(dirname "$(dirname "$init")")"
  if [ -f "$dir/install.sh" ] \
      && [ -x "$dir/.venv/bin/python" ]; then
    CANDIDATES+=("$dir")
  fi
done < <(find "$HOME" -maxdepth 6 -type d -name '.*' -prune -o \
  -type f -path '*/fyrn/__init__.py' -print0 2>/dev/null)

LAUNCHER="$(readlink -f "${BASH_SOURCE[0]}")"

if [ "${#CANDIDATES[@]}" -eq 1 ]; then
  # Exactly one Fyrn folder: that is the move. Point the desktop
  # icons at the new folder, rewrite this launcher to the new
  # folder, and start Fyrn from there.
  NEW_DIR="${CANDIDATES[0]}"
  # Escape the folder for sed's replacement side (&, |, and "
  # would otherwise break the rewrite or the quoted assignment).
  ESCAPED_DIR="${NEW_DIR//&/\\&}"
  ESCAPED_DIR="${ESCAPED_DIR//|/\\|}"
  ESCAPED_DIR="${ESCAPED_DIR//\"/\\\"}"
  for desktop in "$HOME/.local/share/applications/Fyrn.desktop" \
      "$HOME/Desktop/Fyrn.desktop"; do
    if [ -f "$desktop" ]; then
      sed -i "s|^Icon=.*|Icon=$ESCAPED_DIR/assets/fyrn-48.png|" \
        "$desktop"
    fi
  done
  # Rewrite via a temp file and mv, then exec the new launcher
  # straight away: bash reads a running script incrementally, so
  # nothing may run from this file after the overwrite.
  NEW_LAUNCHER="$(mktemp)"
  sed "s|^APP_DIR=.*|APP_DIR=\"$ESCAPED_DIR\"|" "$LAUNCHER" \
    > "$NEW_LAUNCHER"
  chmod +x "$NEW_LAUNCHER"
  mv "$NEW_LAUNCHER" "$LAUNCHER"
  exec "$LAUNCHER" "$@"
fi

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
  MESSAGE="Fyrn moved, and the launcher could not find its new folder. Run bash install.sh again from the Fyrn folder."
else
  MESSAGE="Fyrn moved, and more than one Fyrn folder was found, so the launcher does not know which one to start. Run bash install.sh again from the Fyrn folder you want."
fi
if command -v zenity >/dev/null 2>&1; then
  zenity --error --title="Fyrn" --text="$MESSAGE" 2>/dev/null
else
  echo "$MESSAGE" >&2
fi
exit 1
