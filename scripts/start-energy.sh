#!/bin/sh
set -eu
. "$(dirname "$0")/find-python.sh"
exec "$PYTHON" -m octopus_energy.cli "$@"
