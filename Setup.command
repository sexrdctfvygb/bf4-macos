#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
    printf '%s\n' 'Apple will ask to install Command Line Tools. Complete its dialog; setup will continue automatically.'
    /usr/bin/xcode-select --install
    bf4_waited=0
    while ! /usr/bin/xcode-select -p >/dev/null 2>&1; do
        sleep 10
        bf4_waited=$((bf4_waited + 10))
        if [ "$bf4_waited" -ge 3600 ]; then
            printf '%s\n' 'Tools are not installed yet. Run Setup.command again after Apple finishes.'
            exit 1
        fi
    done
fi
if ! /usr/bin/arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
    printf '%s\n' 'Rosetta 2 is required. Read and accept the Apple terms in the following installer if you agree.'
    /usr/sbin/softwareupdate --install-rosetta
fi
exec /usr/bin/python3 -B src/bf4.py "$@" setup
