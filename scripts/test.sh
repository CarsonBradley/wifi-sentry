#!/bin/bash
# Run the engine test suite: no Wi-Fi, no network beyond 127.0.0.1, never sends alerts.
# Tests marked "hardware" probe this Mac's Wi-Fi for ~1 s; they skip without a built sampler,
# off macOS, or with WIFI_SENTRY_NO_HARDWARE=1 (CI).
set -euo pipefail
cd "$(dirname "$0")/../engine"
if command -v uv >/dev/null 2>&1; then
  exec uv run --locked pytest -q "$@"
fi
exec .venv/bin/python -m pytest -q "$@"
