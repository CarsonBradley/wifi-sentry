#!/bin/bash
# Run the wifi-sentry engine in the foreground (Ctrl-C to stop). Extra args go to sentry.main, e.g.
#   ./run.sh --port 8460 --hosts loopback --data-dir /tmp/ws-test --fake-mac   (a private test instance)
# ./wifi-sentry start runs this in the background; the LaunchAgent (autostart on) runs it under launchd.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x engine/.venv/bin/python ]; then
  echo "run.sh: wifi-sentry is not installed yet (no engine/.venv); run ./install.sh first" >&2
  exit 1
fi
exec engine/.venv/bin/python -m sentry.main "$@"
