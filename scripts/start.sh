#!/bin/sh
set -eu
exec python3 -m daily_briefing.cli "$@"
