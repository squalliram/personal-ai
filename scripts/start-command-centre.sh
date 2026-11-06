#!/bin/sh
# Foreground command centre on 127.0.0.1:8770 by default. Stop with Ctrl-C; no scheduler is created.
set -eu
CDPATH= cd "$(dirname "$0")/.."
. ./scripts/find-python.sh
exec "$PYTHON" -m command_centre serve "$@"
