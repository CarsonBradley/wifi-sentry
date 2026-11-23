# Changelog

All notable changes. Versions follow `version` in `engine/pyproject.toml`.

## 0.3.0 (2026-11-23): phone alerts, arming and setup from the dashboard

Everything a new user needs is now on the dashboard; no file editing.

### Added
- **Phone alerts panel.** Sets up ntfy (generates a random 120-bit topic, shows the subscribe
  steps), webhooks (generic JSON, Slack or Discord format) and email (STARTTLS, SSL or none). Each
  channel has a "Send test alert" button that shows the real result or error, and its last delivery
  status. Settings live in `data/alerts.json` (mode 600); secrets are write-only through the API.
  A channel set up on the dashboard replaces that channel's `[alerts.*]` section from the config
  files; untouched channels keep using the files, and "Remove" returns a channel to them.
- **Exit delay** when arming from the dashboard (default 60 s; none / 30 s / 1 / 2 / 5 min). Motion
  during the delay is logged but never alerted. `POST /api/arm` without `delay_s` still arms at once.
- **Arming schedule**: weekly day + time windows in the Mac's local time, overnight windows,
  DST-aware. Arming or disarming by hand wins until the next window starts or ends; saving a
  schedule never flips the current state. Survives restarts (a boundary or exit delay that passed
  while the engine was down is applied at start).
- **First-run setup guide** on the dashboard: what it is and its limits, sensor check, learning,
  walk test, phone alerts, arming. Can be skipped; resumes across reloads, devices and restarts;
  Settings → "Setup guide" runs it again.
- Failing alert deliveries are shown at the top of the dashboard and in `./wifi-sentry status`.
- API: `/api/arming`, `/api/alerts`, `/api/alerts/settings`, `/api/alerts/channels/{kind}` (+ `/test`),
  `/api/alerts/ntfy-topic`, `/api/onboarding`; status gains `arm` and `alerts`; SSE `alerts` event.
  See [docs/api.md](docs/api.md).
- Config: `[arming] delay_s`, `[alerts] base_url` (dashboard link in alerts), `[alerts.webhook] format`,
  `[alerts.email] security`.
- `./wifi-sentry open`; `./install.sh --help` (unknown options are refused instead of starting an
  install); `./wifi-sentry start` passes engine flags through (e.g. `--learning-s 120`).
- Tools: `fake_esp32.py --key` signs like a board; `udp_dump.py --key` checks signatures;
  `esp32/test/run_host_test.sh --psa` checks the firmware's signing code against Python's hmac.
- Compatibility: a second Mac (MacBook Pro, Apple M5, macOS 27.2.1) installed from a clean copy
  and ran with full two-antenna sensing; see [docs/compatibility.md](docs/compatibility.md).

### Security (from an independent review before the public release)
- **The access token is required on the Mac itself too** (`[auth] loopback_exempt` now defaults to
  false): other programs, macOS accounts and browser extensions could reach 127.0.0.1 and disarm it,
  start walk tests, relearn, re-point alert channels or read the history. New `./wifi-sentry open`
  logs this Mac's browser in; `start`, `url` and the installer print the tokenised local link; the
  CLI reads the token file for its own calls.
- Login cookies are random session ids, expire on the server after 30 days (`cookie_days`, was
  180) and are revoked by logging out; rotating the token ends every session. A correct token is
  always accepted even while its address is rate-limited, and requests from the Mac itself never
  count as failures (no lockout by a local program). The token link always redirects to `/` (no
  open redirect). More proxy headers (`CF-Connecting-IP`, `True-Client-IP`, `X-Client-IP`,
  `X-Forwarded-Port`, ...) disqualify the opt-in loopback exemption.
- **"Not watching" alerts**: while armed, a sensor that is offline, blind, crash-looping or relearning
  for `[alerts] not_watching_s` (120 s) sends one "can't sense" alert and "watching again" when it
  recovers; a down time (engine stopped, Mac asleep) is reported as "was not running" afterwards.
  Own cooldown (`not_watching_cooldown_s`, 1800 s).
- Motion-alert cooldowns are kept per sensor class (Mac / ESP32): data injected on the ESP32 port can
  no longer use up the cooldown and mute the Mac sensor's alert, and the Mac's sensor joining an
  event that ESP32 boards opened is alerted.
- Alert errors name servers as host[:port] only: URLs (with any `user:password@`) and response bodies
  are never echoed to the dashboard, API, log or database; error texts stored by 0.2 are scrubbed.
  URLs with credentials, invalid ports, or loopback / link-local / multicast hosts are refused when
  saved, and targets resolving to them (or to wifi-sentry itself) when sending.
  `[alerts] allow_loopback_targets` (config file only) re-allows a self-hosted ntfy on the same Mac.
- Only the dashboard's own files are served (no longer `web/dev/`).
- Owner-only files: the engine runs with umask 077 and makes `data/` 0700 and its files and
  `config.local.toml` 0600, also for existing installs.
- ESP32 (off by default): optional shared-key signing. Set the same string as menuconfig "Shared
  key" and `[esp32] key`; the board appends `"sig"` (HMAC-SHA256, 128-bit, over the unsigned
  datagram) and the engine drops unsigned or wrongly signed datagrams, with a replay guard while the
  board is live. Not encryption; see SECURITY.md for the limits.
- ESP32: board-name slots expire after 10 min without data (at most 64 names per run), and each name
  is pinned to the IP address that first used it. A board whose Wi-Fi channel keeps changing shows
  "channel keeps changing (possible interference or spoofing)" after 3 changes in 5 min instead of
  being silently left unscored.
- Raw recordings: `[record] max_bytes` is enforced on every write; `[record] include_esp32` now
  defaults to false.
- Sampler: the IOReport subscription is released before resubscribing, and channel lists are
  type-checked element by element.
- CI: `actions/checkout` pinned by commit SHA, without persisted credentials.
- The CLI handles checkout paths with spaces, `&` and other special characters (LaunchAgent
  detection parses the plist; the sampler is matched as a fixed string).
- SECURITY.md and docs/how-it-works.md describe the known detection-integrity limits (channel
  saturation vs. the interference discount, slow movement, the artifact filter, walk tests).

### Changed
- **Dashboard redesign.** The last 24 hours are drawn as a circular day chart, like a chart
  recorder's disc: the pen sits at 12 o'clock with the live reading in the hub, the trigger level is
  a red ring, and the rim marks motion (vermilion) and "not watching" stretches (ochre). The status
  is a plain sentence above it instead of a tinted card; settings, sensors and alerts sit below
  (beside the log on a desktop). No cards, no icon tiles; new app icon. Honesty fixes found on the
  way: the arm switch no longer says "Motion raises an alert" while the sensor is offline or
  learning, a partially covered "All clear" sets its offline sensors apart, the screen-reader live
  region now also announces disconnects, and "Since you last checked" sums up many "not watching"
  gaps in one sentence. Exit delay is a menu. New dev tools for screenshots against a real test
  engine with simulated data: `web/dev/demo_seed.py`, `demo_sampler.py`, `shoot_engine.py`.
- `[alerts] cooldown_s` default 60 → **300 s**. Events held back by the cooldown are counted in the
  next alert instead of being dropped silently. Cooldown counters survive restarts.
- Alert texts rewritten ("Motion detected at home", strength, duration, optional dashboard link).
  The generic webhook payload gains `source`, `title`, `message`, `time`, `url`, `suppressed`; the
  0.2.0 keys `kind`, `event`, `status` are kept.
- Each delivery is retried up to 3 times (2 s / 8 s back-off; not for refused logins or HTTP 4xx
  other than 408/429), with 10 s HTTP / 15 s SMTP timeouts, and never blocks detection.
- ESP32 firmware: no built-in receiver address any more; the board refuses to send until one is
  set in `idf.py menuconfig` (rebuilt for ESP32-S3, -C6, -C3, -C5 and ESP32 with ESP-IDF 6.1.1, zero
  warnings; never run on a board).
- `install.sh` output has no terminal escape codes when it isn't run in a terminal.

### Upgrading from 0.2.0
- **The dashboard asks for the access token on the Mac too**: run `./wifi-sentry open` once. Login
  cookies from 0.2 are no longer valid, so phones need the link from `./wifi-sentry url` once more
  (the token itself is unchanged). Scripts calling the API need `Authorization: Bearer <token>`.
- `[record] include_esp32` now defaults to false; set it to true to keep recording ESP32 frames.
- `./wifi-sentry update` (pulls, rebuilds the sampler, updates Python packages and restarts the
  engine if it was running). An install that has already recorded events doesn't get the setup guide; it shows a dismissible "Set up phone alerts" prompt
  (hidden once any channel is on). Alert channels in `config.local.toml` keep working unchanged.
- The cooldown default changed to 300 s; set `[alerts] cooldown_s = 60` in `config.local.toml` (or
  use the dashboard) to keep the old behaviour.

Verified with local stub HTTP/SMTP servers, headless-browser flows against a real second engine, and
one real publish to ntfy.sh. Not yet verified: a real phone receiving the push, real SMTP providers,
Slack / Discord / Home Assistant endpoints, iOS Safari itself.

## 0.2.0 (2026-11-23): installable on other Macs, access token

Made the single-machine build safe to clone and run elsewhere.

### Added
- `./install.sh`: checks macOS, the Wi-Fi interface, Command Line Tools and uv (asks before
  installing uv), builds the sampler, sets up Python with `uv sync --locked`, creates
  `config.local.toml` and the access token, runs a 2-second probe, checks the port. Safe to re-run.
- `./wifi-sentry` command line: `start`, `stop`, `restart`, `status`, `url`, `doctor`, `logs`,
  `probe`, `autostart on|off` (LaunchAgent generated for the checkout), `update`,
  `uninstall [--purge]`, `version`, `run`.
- Access token for everything that isn't the Mac itself: Bearer header or an HttpOnly,
  SameSite=Strict login cookie set from a `?token=` link or the dashboard's "Enter access token"
  form. Per-address rate limit, constant-time comparison, tokens never logged. The dashboard shows a
  locked screen on 401 and discards all displayed data.
- `[server] expose = ["tailscale" | "lan" | address]`: 127.0.0.1 is always bound; Tailscale and
  Wi-Fi addresses are looked up at start and retried until available. Wildcards are refused.
- Sampler capabilities line (`{"type":"caps"}`), `--probe`, `--iface`, `--version`; the Wi-Fi
  interface is found through CoreWLAN instead of a fixed name; private calls are type-checked
  before use; universal (arm64 + x86_64) build.
- Signal modes for Macs that expose a different number of antennas: 3–4 antennas generalised,
  1 antenna "Reduced sensing", none "Degraded sensing" (smoothed overall signal), Wi-Fi off /
  disconnected / absent reported as such. Only the 2-antenna path has run on real hardware; the
  others are tested with simulated sampler output.
- `--fake-mac [MODE]` engine flag: replays a bundled recording instead of the real sampler.
- Security headers (`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff`).

### Changed
- `config.toml` holds generic defaults only; machine settings and secrets go in git-ignored
  `config.local.toml` (template `config.local.example.toml`).
- `[server] hosts` is deprecated in favour of `expose`; an existing list still works and `doctor`
  notes it.
- The ESP32 UDP listener is **off by default** and binds to the Wi-Fi address (`bind = "lan"`) when
  enabled.
- Two-antenna scoring is unchanged (identical results on recorded data).
- Single version source: `engine/pyproject.toml`, stamped into the sampler at build time.

### Removed
- Hard-coded addresses, paths, interface name and the fixed LaunchAgent plist.

### Fixed (from the first night with 0.1.0)
- **Radio "box" artifact.** The first overnight run logged 27 events in about 2.5 hours; 25 were a
  short (under ~1 s) +3/+4 dB jump on both antennas together. The detector now drops such
  common-mode boxes before scoring (`[detect] mac_pulse_reject`, on by default). Replaying the
  night: low 13 → 1, medium 30 → 3, high 42 → 6 events. Detection of simulated walking on the idle
  recording was unchanged. Details: [docs/how-it-works.md](docs/how-it-works.md#5-the-first-night-a-radio-artifact-that-looked-like-motion).
- **False "sensor offline" gaps.** Unchanged readings while packets keep arriving are a still room,
  not a blind sensor. "Frozen" now needs the packet counter to stall too; unchanged for 10 minutes
  despite traffic is reported as "stuck" (`[sources] mac_stuck_s`).

## 0.1.0 (2026-11-23): overnight build

First working version, built and run on one Mac mini.

- `wifisense`: Swift sampler reading per-antenna RSSI (private CoreWiFi), noise, MCS, packet
  counters and driver statistics (IOReport) at 10 Hz, without sudo or permissions.
- Engine: step-robust variance of the two antennas' readings over 3 s, adaptive robust baseline
  (10-minute learning, median/MAD, motion never learned, steady disturbances absorbed),
  interference discount, events with hysteresis, three sensitivity levels, walk test with a
  recommendation, coverage gaps ("Not watching"), SQLite storage, raw recordings, replay tool and
  injected-motion simulator.
- Alerts: ntfy, webhook, email; only while armed, with a cooldown.
- Dashboard: mobile-first, light and dark, live over Server-Sent Events, events with Real / False
  alarm labels, activity chart, "Since you last checked" summary, no external requests.
- ESP32 CSI firmware (ESP-IDF 6.1.1; compiles for ESP32-S3, -C6, -C5, -C3 and ESP32, never run on
  a board), a board simulator and a UDP inspection tool.
