#!/bin/sh
# Run from the application checkout, while holding its deployment lock.
set -eu
. "$(dirname "$0")/find-python.sh"
"$PYTHON" -m octopus_energy.refresh_install --app-path "$(pwd -P)" \
  --target "${DASHBOARD_TARGET:?Set DASHBOARD_TARGET to the protected web directory}" \
  --interval "${ENERGY_REFRESH_INTERVAL_MINUTES:-60}"
