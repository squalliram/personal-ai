#!/bin/sh
# Run from the application checkout, while holding its deployment lock.
set -eu
. "$(dirname "$0")/find-python.sh"
"$PYTHON" -m instagram_agent.cron_install --app-path "$(pwd -P)"
