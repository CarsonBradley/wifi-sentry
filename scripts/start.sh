#!/bin/bash
# Kept for 0.1.x habits: same as ./wifi-sentry start
exec "$(dirname "$0")/../wifi-sentry" start "$@"
