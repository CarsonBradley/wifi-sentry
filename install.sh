#!/bin/bash
# wifi-sentry installer. Safe to run again (it only rebuilds and re-checks). Never uses sudo,
# never changes system settings, never scans or toggles Wi-Fi.
#   ./install.sh          check this Mac, build the sampler, set up Python, create local config
#   ./install.sh --yes    don't ask before installing uv (for scripted installs)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"
YES=""
case "${1:-}" in
  "") ;;
  -y|--yes) YES=1 ;;
  -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "install.sh: unknown option $1 (try --help)" >&2; exit 2 ;;
esac

bold() { if [ -t 1 ]; then printf '\033[1m%s\033[0m\n' "$*"; else printf '%s\n' "$*"; fi; }
step() { echo; bold "==> $*"; }
ok() { echo "    ok: $*"; }
warn() { echo "    warning: $*"; }
die() { echo; echo "install.sh: $*" >&2; exit 1; }

step "Checking this Mac"
[ "$(uname -s)" = "Darwin" ] || die "wifi-sentry runs on macOS only (it reads the Mac's own Wi-Fi link)."
OSV=$(sw_vers -productVersion)
MAJOR=${OSV%%.*}
ARCH=$(uname -m)
[ "$MAJOR" -ge 11 ] 2>/dev/null || die "macOS $OSV is too old: wifi-sentry needs macOS 11 (Big Sur) or later."
MODEL=$(sysctl -n hw.model 2>/dev/null || echo "?")
ok "macOS $OSV on $ARCH ($MODEL)"
if [ "$ARCH" = "x86_64" ] && [ "$MAJOR" -lt 13 ]; then
  warn "Intel Mac on macOS $OSV: the sampler is built for your exact system below; untested on Intel."
fi
WIFI_DEV=$(networksetup -listallhardwareports 2>/dev/null | awk '/^Hardware Port: (Wi-Fi|AirPort)$/{getline; print $2; exit}')
if [ -n "$WIFI_DEV" ]; then
  ok "Wi-Fi interface $WIFI_DEV"
else
  warn "no Wi-Fi interface found. wifi-sentry senses motion through this Mac's Wi-Fi link, so the Mac"
  warn "itself can't be a sensor; only ESP32 boards (esp32/README.md) would work. Continuing anyway."
fi

step "Command Line Tools (to build the Swift sampler)"
if xcode-select -p >/dev/null 2>&1 && xcrun --find swiftc >/dev/null 2>&1; then
  ok "$(xcrun swiftc --version 2>/dev/null | head -1)"
else
  cat >&2 <<'EOF'

The Xcode Command Line Tools are needed to build the Wi-Fi sampler (full Xcode is not required).
Install them with:

    xcode-select --install

A dialog opens; the download is a few hundred MB to ~1.5 GB. Then run ./install.sh again.
EOF
  exit 1
fi

step "uv (Python environment manager)"
UV=""
for c in "$(command -v uv 2>/dev/null || true)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if [ -n "$c" ] && [ -x "$c" ]; then UV="$c"; break; fi
done
if [ -z "$UV" ]; then
  echo "    uv is not installed. It sets up the Python version and packages wifi-sentry needs, inside"
  echo "    this folder only. The official installer (https://docs.astral.sh/uv/) is:"
  echo
  echo "        curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo
  ans=n
  if [ -n "$YES" ]; then ans=y
  elif (exec < /dev/tty) 2>/dev/null; then read -r -p "    Run it now? [y/N] " ans < /dev/tty || ans=n
  else echo "    (no terminal to ask on; not installing it)"; fi
  case "${ans:-n}" in
    y|Y|yes)
      curl -LsSf https://astral.sh/uv/install.sh | sh
      UV="$HOME/.local/bin/uv"
      [ -x "$UV" ] || die "uv was not installed where expected ($UV); install it yourself and run ./install.sh again."
      ;;
    *) die "uv is required. Install it (command above, or: brew install uv) and run ./install.sh again." ;;
  esac
fi
ok "$("$UV" --version)"

step "Building the sampler (sensor/wifisense)"
sh sensor/build.sh || die "building the sampler failed (see above)."

step "Python environment (engine/.venv)"
echo "    uv picks a Python 3.12+ it finds, or downloads one into its own cache (no system changes)."
(cd engine && "$UV" sync --locked) || die "uv sync failed. If uv says the lockfile is unsupported, update uv (uv self update) and retry."
ok "$(engine/.venv/bin/python --version)"

step "Local configuration"
if [ -f config.local.toml ]; then
  ok "config.local.toml exists (kept as is)"
else
  cp config.local.example.toml config.local.toml
  ok "created config.local.toml from config.local.example.toml (edit it for your setup)"
fi
chmod 600 config.local.toml   # it may hold alert passwords
mkdir -p data && chmod 700 data
engine/.venv/bin/python -m sentry.cli info >/dev/null || die "the configuration has an error (see above)."
engine/.venv/bin/python -m sentry.cli token >/dev/null 2>&1 && ok "access token ready (data/access-token; ./wifi-sentry url shows it)"

step "What this Mac's Wi-Fi provides (2-second probe)"
set +e
engine/.venv/bin/python -m sentry.cli probe
PROBE=$?
set -e

step "Port"
if engine/.venv/bin/python -m sentry.cli pid >/dev/null 2>&1; then
  ok "wifi-sentry is already running from this folder (./wifi-sentry restart picks up the new build)"
elif engine/.venv/bin/python -m sentry.cli port-check; then
  ok "port $(engine/.venv/bin/python -m sentry.cli info | sed -n 's/^port=//p') is free"
fi

echo
bold "Installed wifi-sentry $(engine/.venv/bin/python -m sentry.cli info | sed -n 's/^version=//p')."
if [ "$PROBE" -ne 0 ]; then
  echo "This Mac can't sense motion right now (see the probe above). Fix that, then start it."
fi
cat <<EOF
Next:
  ./wifi-sentry start          start it in the background
  ./wifi-sentry open           the dashboard on this Mac (first 10 minutes: learning, keep still)
  ./wifi-sentry url            links with the access token (phone: needs expose = ["tailscale"])
  ./wifi-sentry autostart on   start at login
  ./wifi-sentry doctor         if anything looks wrong
EOF
