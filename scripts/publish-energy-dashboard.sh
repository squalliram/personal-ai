#!/bin/sh
# Publish only the generated, identifier-free dashboard files. Run on cPanel
# after its directory protection and server-side .env have been configured.
set -eu

dashboard_dir="${DASHBOARD_DIR:-reports/energy/dashboard}"
target_dir="${DASHBOARD_TARGET:?Set DASHBOARD_TARGET to the protected web directory}"

[ -f "$dashboard_dir/index.html" ]
[ -f "$dashboard_dir/data.json" ]
mkdir -p "$target_dir"
cp "$dashboard_dir/index.html" "$dashboard_dir/data.json" "$target_dir/"
