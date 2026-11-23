# Security

wifi-sentry is a hobby-grade tool for your own home network. This page says plainly what it
protects against and what it doesn't.

## Threat model in plain terms

**The dashboard and API**

- By default the engine listens on **127.0.0.1 only**: nothing else on your network can reach it.
- **Every API request needs the access token**, including requests from the Mac itself: other
  programs, other macOS accounts and browser extensions can all reach 127.0.0.1, and without the
  token they could disarm it, start walk tests (which suppress events), relearn, re-point alert
  channels or read the event history. The token is `data/access-token` (mode 600). The CLI reads it
  for its own calls; `./wifi-sentry open` logs this Mac's browser in, and `./wifi-sentry url` prints
  links for other devices. The dashboard's static files (no data in them) are served without it;
  only the dashboard's own files are served (nothing from `web/dev/` or anywhere else).
- If you add `[server] expose = ["tailscale"]` or `["lan"]`, it also listens on that address, with
  the same token requirement.
- **Login cookie:** opening a token link once (or the "Enter access token" form) sets a cookie that
  holds a random session id bound to the token, not the token. It is HttpOnly and SameSite=Strict,
  expires on the server after 30 days (`[auth] cookie_days`), and "log out" revokes it on the
  server. Rotating the token (delete `data/access-token`, restart) ends every session. Browsers
  scope cookies by host, not by port: another web app on the same host name and a different port
  can see the cookie, so don't run untrusted web apps on the same address.
- The token link always redirects to `/` (no open redirect), and the token is removed from the
  address bar. Wrong tokens are rate-limited per address (10 per 5 minutes; never for requests from
  the Mac itself, so a local program can't lock you out), a correct token is always accepted (its
  192 random bits are what stops guessing), tokens are compared in constant time and never logged.
- `[auth] loopback_exempt = true` brings back the 0.2 behaviour (requests from the Mac itself need
  no token). Only use it on a Mac nobody else uses. Requests carrying proxy headers
  (`X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP`, `True-Client-IP`, Tailscale identity headers
  and others) or a non-loopback `Host` are never exempt, but port forwards that add no headers
  (`ssh -L`, `socat`, `tailscale serve --tcp`, Docker port maps) make remote users look local.
- It is **plain HTTP**. On your LAN, anyone who can watch your Wi-Fi traffic could read the cookie
  and the dashboard data. Tailscale encrypts the traffic between your devices, which is why it is
  the recommended way to reach the dashboard from a phone.
- **It is not built to face the internet.** Don't port-forward it, and don't put it behind a public
  reverse proxy.
- Wildcard listen addresses (`0.0.0.0`, `::`) are refused for the dashboard.
- Browser protections: `Host` header check against DNS rebinding, cross-origin state-changing
  requests refused (another website can't disarm it), no CORS headers, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `nosniff`.
- Anyone with the token can see when motion happens in your home and can disarm alerts. Treat it
  like a house key. Rotate it by deleting `data/access-token` and restarting.

**The ESP32 UDP port (off by default)**

- With `[esp32] enabled = true` the engine accepts sensor data on UDP port 5566. **Unless you set
  a shared key, that port is unauthenticated**: anyone who can send packets to it can inject fake
  sensor data, causing false alarms or making an ESP32 source look quiet. They can't affect the
  Mac's own sensor or the dashboard.
- **Shared key** (`[esp32] key` in `config.local.toml`, and the same string as *Shared key* in each
  board's menuconfig): every datagram then carries an HMAC-SHA256 signature (truncated to 128 bits)
  and the engine drops, and counts, anything unsigned or wrongly signed. What it does **not** do:
  it doesn't encrypt anything (anyone on your network can still read the signal data, which shows
  when someone moves); one key is shared by all boards and the Mac, so whoever has one board's
  flash or `esp32/sdkconfig` can sign as any board; and it can't stop jamming or dropped packets.
- **Replays** (key set only): each board's uptime counter must move forward in step with the
  Mac's clock, so a recorded datagram can't be sent again while the board is live. The guard has
  limits: after an engine restart, or once a board has been silent for 30 s, the next correctly
  signed datagram is trusted, so a recording replayed at that moment is accepted until the real
  board resumes.
- **Board names**: at most 16 hold a slot at once, and a name with no accepted datagram for 10
  minutes frees its slot (at most 64 names per engine run). Each name is pinned to the IP address
  that first used it; the same name from another address is dropped. IP addresses are easy to
  spoof on a LAN, so pinning only stops the casual case; the key is the real protection.
- **Channel changes**: a board's score pauses for a few seconds after its Wi-Fi channel changes.
  Only the first 3 changes in 5 minutes do that; beyond that the dashboard shows the board as
  "channel keeps changing (possible interference or spoofing)" instead of silently not scoring it.
- It binds to the Mac's Wi-Fi address by default (`bind = "lan"`), not every interface. Malformed
  datagrams are dropped and counted and datagrams are capped at 1400 bytes. ESP32 frames are not
  written to the raw recordings unless `[record] include_esp32 = true`, and the recordings never
  exceed `[record] max_bytes`, even under a flood. Keep untrusted devices on a guest network.
- The firmware has never run on real hardware; treat this path as experimental.

**Alerts**

- Alerts leave your network by design. With the public ntfy.sh server, anyone who knows the topic
  can read the alerts; the dashboard generates a long random topic, and alert texts contain no names
  or sensor details (and no dashboard link unless you set one).
- Alert secrets (ntfy token, webhook URL, SMTP password) are write-only through the API: it only
  reports whether a secret is set. Delivery errors name the server as host and port only; URLs
  (webhook URLs and ntfy topics are secrets themselves) and server response bodies are never
  echoed to the dashboard, the API, the log or the database. Error texts stored by older versions
  are scrubbed at start.
- URLs with a `user:password@` part, an invalid port, or a loopback, link-local or multicast host
  are refused when saved, and names that resolve to such addresses (or to wifi-sentry's own
  listening address) are refused when sending, so a webhook can't be aimed at other services on
  the Mac or at a cloud metadata address. Addresses on your LAN (Home Assistant) are fine. A
  self-hosted ntfy on the same Mac needs `[alerts] allow_loopback_targets = true` in
  `config.local.toml` (it can't be switched on through the API).
- **While armed, wifi-sentry tells you when it stops watching**: a sensor offline, a blind or
  crash-looping sampler, or relearning for more than 2 minutes sends one "can't sense" alert and a
  "watching again" one when it recovers; after the engine was down (Mac asleep, crash, power cut) it
  sends "was not running" at the next start. It **cannot** report anything when the Mac or your
  internet connection is down at that moment. If that matters, add an outside check: for example a
  monitoring service that expects a regular webhook, or a Home Assistant automation that notices
  when the dashboard stops answering.
- Motion alerts have a cooldown per sensor class (Mac / ESP32), so data injected through the ESP32
  port can't use up the cooldown and mute an alert from the Mac's own sensor, and the Mac's sensor
  joining an event that ESP32 boards opened sends an alert of its own.

**Local data and secrets**

- `data/` holds the event history, raw signal recordings, the token, login sessions and alert
  settings. A history of motion is also a history of when people are home. It never leaves the Mac
  unless you copy it. The engine runs with umask 077 and makes `data/` (0700), everything in it and
  `config.local.toml` (0600) readable by your user only, also for installs made by older versions.
- Credentials written by hand go in `config.local.toml`, which is git-ignored.
- `esp32/sdkconfig` holds your Wi-Fi password (and the ESP32 shared key) after `idf.py menuconfig`
  and is git-ignored.
- The sampler uses private macOS APIs but needs no sudo, entitlements, or Location Services
  permission, and never changes system settings.

**Detection integrity (what someone nearby could do)**

A radio motion detector can be fooled by anyone who controls the radio around it. Known limits:

- **Jamming or saturating the channel.** Heavy interference raises the noise floor and channel busy
  time, and the interference discount then scales the Mac's score down (never below half) to avoid
  false alarms. Someone who floods the channel on purpose can use that to make real motion score
  lower. The dashboard doesn't flag it yet.
- **Slow or box-shaped movement.** The score measures fluctuation over 3 seconds and the baseline
  slowly absorbs steady disturbances; very slow, steady movement may stay under the trigger. The
  artifact filter drops short, identical rises on both antennas, so a disturbance that produces
  exactly that shape isn't scored.
- **Walk test.** While a walk test runs (at most 3 minutes, and it can be started again) no events
  open. Starting one needs the token.
- **Learning.** For 10 minutes after a start or "Relearn baseline" nothing is detected (while armed,
  that sends a "can't sense" alert after 2 minutes).

**Not in scope:** an attacker who already has your user account on the Mac (they can read the
token); physical attacks; jamming or otherwise interfering with Wi-Fi to blind the sensor beyond the
limits above (a radio-based detector can't defend against that); and using wifi-sentry as a
certified security system, which it isn't.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting**: on the repository page go to
**Security → Report a vulnerability**. That reaches the maintainer privately. Don't open a public
issue for anything that could put users' homes or networks at risk.

Include what you found, how to reproduce it, and which version (`./wifi-sentry version`). This is a
one-person project, so responses may take a few days. There's no bug bounty.

## Supported versions

Only the latest release gets fixes.
