#!/bin/bash
# Replay recorded JSONL (or the simulator with --sim) through the detection code. See --help.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARGS=()
for a in "$@"; do
  if [ -e "$a" ]; then ARGS+=("$(cd "$(dirname "$a")" && pwd)/$(basename "$a")"); else ARGS+=("$a"); fi
done
cd "$ROOT"
exec engine/.venv/bin/python -m sentry.replay "${ARGS[@]+"${ARGS[@]}"}"
