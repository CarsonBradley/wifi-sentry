#!/bin/bash
# Kept for 0.1.x habits: same as ./wifi-sentry autostart on (--uninstall: autostart off)
if [ "${1:-}" = "--uninstall" ]; then exec "$(dirname "$0")/../wifi-sentry" autostart off; fi
exec "$(dirname "$0")/../wifi-sentry" autostart on
