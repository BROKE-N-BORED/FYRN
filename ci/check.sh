#!/bin/sh
# Free CI entry point - runs the vendored local-ci check.
# Installed by BNB-TOOLS T28 local-ci (setup). Usage:
#   sh ci/check.sh        (from anywhere; the check finds the repo)
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/local_ci.py" check --repo "$DIR/.."
