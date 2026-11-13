#!/bin/sh
# One daily-drafts tick. Scheduling is installed separately (scripts/install-instagram-daily.sh).
set -eu
CDPATH= cd "$(dirname "$0")/.."
. ./scripts/find-python.sh
exec "$PYTHON" -m instagram_agent daily "$@"
