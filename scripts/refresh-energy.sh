#!/bin/sh
# One foreground refresh tick. Scheduling is installed separately.
set -eu
CDPATH= cd "$(dirname "$0")/.."
. ./scripts/find-python.sh
exec "$PYTHON" -m octopus_energy.refresh "$@"
