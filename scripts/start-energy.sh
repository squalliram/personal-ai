#!/bin/sh
set -eu
# Use $PYTHON if set, else the first Python >= 3.10 found (CloudLinux/cPanel ships these under /opt/alt).
if [ -z "${PYTHON:-}" ]; then
  for candidate in python3 python3.13 python3.12 python3.11 python3.10 \
    /opt/alt/python313/bin/python3 /opt/alt/python312/bin/python3 \
    /opt/alt/python311/bin/python3 /opt/alt/python310/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      PYTHON=$candidate
      break
    fi
  done
fi
if [ -z "${PYTHON:-}" ]; then
  echo "start-energy.sh: Python 3.10 or newer is required (set PYTHON to its path)." >&2
  exit 1
fi
exec "$PYTHON" -m octopus_energy.cli "$@"
