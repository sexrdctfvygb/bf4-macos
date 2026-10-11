#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec /usr/bin/python3 -B src/bf4.py "$@" menu
