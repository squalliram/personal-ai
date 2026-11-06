#!/bin/sh
# Publish only the explicit dashboard assets to a protected cPanel directory.
set -eu
. "$(dirname "$0")/find-python.sh"
exec "$PYTHON" -m octopus_energy.publisher --source "${DASHBOARD_DIR:-reports/energy/dashboard}" \
  --target "${DASHBOARD_TARGET:?Set DASHBOARD_TARGET to the protected web directory}" \
  --app-path "$(pwd -P)" "$@"
