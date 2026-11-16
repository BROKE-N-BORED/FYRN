#!/bin/sh
# Run Fyrn's test suite the way the sandbox needs it: the shared
# venv's Python (it has PySide6 and llama-cpp-python) and the
# extracted Qt libraries, when they exist. Anywhere else it falls
# back to plain python3 with no extra library path. Invoked by
# local CI (ci/config.json) and runnable on its own:
#   sh ci/run_suite.sh
if [ -x "$HOME/workspace/forge-v2-testenv/bin/python" ]; then
  PY="$HOME/workspace/forge-v2-testenv/bin/python"
else
  PY="python3"
fi
if [ -d "$HOME/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu" ]; then
  LD_LIBRARY_PATH="$HOME/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export LD_LIBRARY_PATH
fi
QT_QPA_PLATFORM=offscreen
export QT_QPA_PLATFORM
exec "$PY" -m unittest discover -s tests
