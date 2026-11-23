# HTTP API and event stream

The engine serves the dashboard and a small JSON API on `http://127.0.0.1:8450` (plus any
addresses in `[server] expose`). Everything here is derived from `engine/src/sentry/app.py`,
`detector.py`, `storage.py`, `arming.py`, `alertcfg.py` and `sinks.py` (version 0.3.0). It's the
contract the dashboard in `web/` uses; there is no API versioning yet, so expect changes before 1.0.

## Access

- Every `/api/*` request needs the access token from `data/access-token`, also from the Mac itself
  (`./wifi-sentry url` prints it inside the links):
  - `Authorization: Bearer <token>`, or
  - the session cookie `wifi_sentry_<port>`, set by opening any dashboard page once with
    `?token=<token>` (the server answers `303` with `Location: /`) or by `POST /api/login`. The
    cookie holds a random session id; sessions expire on the server after `[auth] cookie_days`
    (30) and `POST /api/logout` revokes the current one.
- `[auth] loopback_exempt = true` (opt-in, see [SECURITY.md](../SECURITY.md)) exempts requests from
  the Mac itself (to 127.0.0.1, with a `localhost` / `127.0.0.1` / `::1` `Host` header and no proxy
  headers).
- Open without a token: the dashboard's static files (only files directly in `web/` with a known
  type; they contain no data), `GET /api/auth`, `POST /api/login`, `POST /api/logout`.
- Wrong tokens: after `max_failures` (10) from one address within `failure_window_s` (300 s),
  further wrong tokens from that address get `429` with `Retry-After` and
  `{"error": "...", "retry_after": N}`. A correct token is always accepted, and requests from the Mac
  itself are never counted.
- Unauthenticated `/api/*` requests get `401` `{"error": "access token required", "auth": "required"}`
  with `WWW-Authenticate: Bearer realm="wifi-sentry"`.
- State-changing requests (anything but GET/HEAD/OPTIONS) with an `Origin` header that doesn't
  match the `Host` are refused with `403`. No CORS headers are ever sent.
- Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`.

Errors are JSON `{"error": "message"}`: `400` for bad input, `404` for unknown `/api/` paths or
events. Request bodies are JSON objects, at most 64 KB.

Example from another device:

```sh
TOKEN=$(cat data/access-token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8450/api/status      # on the Mac
curl -H "Authorization: Bearer $TOKEN" http://<this-mac>:8450/api/status     # from another device
```

## Endpoints

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/auth` | | `{"local": bool, "required": bool, "authenticated": bool}` (`local`: exempt by `loopback_exempt`) |
| POST | `/api/login` | `{"token": "..."}` | `{"ok": true}` and sets the cookie; `401` on a wrong token |
| POST | `/api/logout` | | `{"ok": true}`, revokes the session and clears the cookie |
| GET | `/api/status` | | [status object](#status-object) |
| GET | `/api/events` | `?limit=50` (1 to 1000), `?before=<id>` (page back) | `{"events": [event, ...]}`, newest first |
| POST | `/api/events/{id}/label` | `{"label": "real" \| "false" \| null, "note": "..."}` (`note` optional, cut at 2000 characters) | the updated [event](#event-object) |
| GET | `/api/history` | `?seconds=3600` (10 s to 30 days), `?points=600` (10 to 5000) | [history object](#history-object) |
| GET | `/api/gaps` | `?since=<unix s>` (default 24 h ago, at most 31 days back), `?until=<unix s>` (default now) | `{"gaps": [gap, ...]}` |
| POST | `/api/arm` | `{"armed": true \| false, "delay_s": 0-3600}` (`delay_s` optional: without it, or with 0, it arms at once; the dashboard sends its exit delay) | status object |
| GET / POST | `/api/arming` | POST, any of: `{"delay_s": 0-3600, "schedule": {"enabled": bool, "windows": [window, ...]}}` (other fields refused) | [arming object](#arming-object) |
| GET | `/api/alerts` | | [alerts object](#alerts-object) |
| POST | `/api/alerts/settings` | any of `{"cooldown_s": 0-86400, "notify_on_close": bool, "base_url": "http(s)://..." \| ""}` | alerts object |
| POST | `/api/alerts/channels/{ntfy\|webhook\|email}` | partial update with the channel's [settings fields](#alerts-object); a secret that is null or omitted is kept, `""` clears it. Turning a channel on with required fields missing is refused (`400`) | alerts object |
| DELETE | `/api/alerts/channels/{kind}` | | alerts object; the channel goes back to the config file's `[alerts.<kind>]` settings (or off) |
| POST | `/api/alerts/channels/{kind}/test` | | [delivery result](#delivery-result) of one test alert (no retries, sent even if the channel is off); `400` if the channel isn't set up |
| POST | `/api/alerts/ntfy-topic` | | `{"topic": "sentry-..."}`, a random 120-bit suggestion; nothing is saved |
| GET / POST | `/api/onboarding` | POST, any of: `{"status": "active" \| "done" \| "skipped", "step": "<one of steps>", "alerts_prompt_dismissed": bool}` | [onboarding object](#onboarding-object) |
| POST | `/api/sensitivity` | `{"level": "low" \| "medium" \| "high"}` | status object |
| POST | `/api/relearn` | | status object (closes an open event, clears baselines, state becomes `learning`) |
| POST | `/api/walktest` | `{"action": "start" \| "stop"}` | [walk test object](#walk-test-object) |
| GET | `/api/walktest` | | walk test object |
| GET | `/api/stream` | | Server-Sent Events, see [below](#event-stream-apistream) |
| GET | `/api/debug` | | diagnostics (unstable): detector internals and baselines, last 50 alert deliveries, sinks, sampler pid / restarts / last error, ESP32 listener counters (received, invalid, unsigned, bad signature, replayed, wrong IP, expired names), bound addresses, auth settings, config notes and files, version |

## Status object

```jsonc
{
  "sensing": "full",          // full | reduced | degraded | none (best online source)
  "state": "clear",           // learning | clear | motion | offline
  "armed": false,
  "since": 1795000000.0,      // when `state` started (unix s)
  "score": 0.42,              // current fused score
  "threshold": 3.0,           // trigger level of the current sensitivity
  "sensitivity": "medium",
  "learning_progress": 1.0,   // 0..1
  "learning_eta_s": null,     // seconds left while learning, else null
  "sources": [ /* source objects */ ],
  "uptime_s": 3600.0,
  "version": "0.3.0",
  "thresholds": {"low": 5.0, "medium": 3.0, "high": 2.0},
  "walktest_running": false,
  "event_id": null,           // id of the open event, if any
  "arm": { /* arm state, see the arming object */ },
  "alerts": {"channels": ["ntfy"],          // enabled channels
             "failing": []}                 // [{"channel", "label", "t", "error"}]: latest real delivery failed
}
```

Every source object has:

| Field | Meaning |
|---|---|
| `id` | `"mac"` or `"esp32:<name>"` |
| `kind` | `"mac"` or `"esp32"` |
| `online` | data within `offline_after_s` |
| `rate_hz` | samples per second over the last 5 s |
| `last_seen` | unix s of the last sample, or null |
| `score` | this source's current score |
| `rssi` | dBm (Mac: mean of the antennas), or null |
| `learning`, `learning_progress` | baseline state of this source |
| `health` | null, or why the source can't be scored right now (e.g. `"Wi-Fi is turned off on this Mac"`, `"per-chain RSSI frozen (no link traffic?)"`) |
| `aux_factor` | interference discount applied (1.0 = none) |

Mac sources add `rssi_c0`, `rssi_c1`, `rssi_chains` (list), `channel`, `channel_changes`,
`signal_mode` (`dual` / `multi` / `single` / `aggregate` / `nolink`), `notice` and `notice_level`
(a reduced / degraded explanation, or null), `link` (`up` / `disconnected` / `off` / `none`), and
`sampler` (`{sampler, os, model, iface, per_antenna, chains}` from the sampler's capabilities line).

ESP32 sources add `fmt` (CSI layout), `channel`, `lost_pct`, `reboots`.

## Event object

```jsonc
{
  "id": 12,
  "start": 1795000000.123,
  "end": 1795000033.456,      // null while open
  "duration_s": 33.3,
  "peak_score": 8.01,
  "threshold": 3.0,           // trigger level in force when it peaked
  "sources": ["mac"],
  "armed": false,             // armed when it opened
  "label": null,              // "real" | "false" | null
  "note": "",
  "walktest": false
}
```

## History object

`/api/history` buckets the 1 Hz score history into at most `points` buckets (maximum per bucket):

```jsonc
{
  "t": [ ... ],               // bucket end times (unix s)
  "score": [ ... ],
  "threshold": [ ... ],
  "per_source": {"mac": [ ... ]},   // aligned with t; null where a source had no score
  "gaps": [ /* gap objects in the range */ ]
}
```

A **gap** is a stretch when nothing was watched: `{"start", "end", "reason", "ongoing"?}` with
`reason` one of `engine_down`, `source_offline`, `learning`. `ongoing: true` marks one still in
progress (its `end` is the query's end).

## Walk test object

```jsonc
// while running
{"running": true, "started": 1795000000.0, "result": null, "finished": null,
 "live": {"elapsed_s": 12.0, "peak_score": 4.1, "median_score": 1.9}}

// after it stops (result of the most recent test, kept across restarts)
{"running": false, "started": ..., "finished": ..., "result": {
  "started", "finished", "duration_s", "n_ticks",
  "peak_score", "median_score",
  "idle_median", "idle_p99",                 // null if the idle baseline isn't known yet
  "threshold_by_level": {"low": 5.0, "medium": 3.0, "high": 2.0},
  "detected_by_level": {"low": false, "medium": true, "high": true},
  "fraction_above_by_level": {"low": 0.0, "medium": 0.31, "high": 0.52},
  "per_source_peak": {"mac": 4.1},
  "recommended": "medium",                   // low | medium | high | none
  "message": "human-readable summary",
  "raw_path": "path of the raw samples recorded during the test"
}}
```

A walk test stops by itself after `[walktest] max_duration_s` (180 s).

## Arming object

`GET /api/arming` and the answer to `POST /api/arming`:

```jsonc
{
  "settings": {
    "delay_s": 60,                              // exit delay used by the dashboard's arm switch
    "schedule": {"enabled": true, "windows": [
      // days: 0 = Monday .. 6 = Sunday, the day the window starts; HH:MM in the Mac's local time.
      // end before start runs past midnight; end "24:00" is allowed; at most 14 windows
      {"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:30"}
    ]}
  },
  "state": {                                    // also in the status object as "arm"
    "armed": false,
    "mode": "schedule",                         // manual | schedule | delay (exit delay running)
    "target": false,                            // what it is heading to (true during the exit delay)
    "arm_at": null,                             // when the exit delay ends (unix s), else null
    "delay_s": 60,
    "schedule_on": true,
    "scheduled": false,                         // what the schedule says right now (null without one)
    "override_until": null,                     // a manual setting lasts until this boundary (unix s)
    "next_change": {"t": 1795500000.0, "armed": true}   // next flip within a week, or null
  },
  "timezone": "Europe/London"
}
```

Arming or disarming by hand wins until the schedule's next window start or end. Saving a schedule
never flips the current state; it takes over at its next boundary.

## Alerts object

`GET /api/alerts` and the answer to every alert-settings change. Secrets are never returned, only
whether they are set.

```jsonc
{
  "settings": {"cooldown_s": 300.0, "notify_on_close": false, "base_url": ""},
  "channels": [
    {"id": "ntfy", "label": "Phone (ntfy)",
     "source": "dashboard",                     // dashboard | config_file | none
     "configured": true, "enabled": true,
     "complete": true,                          // required fields present
     "secrets": {"token": false},               // ntfy: token; webhook: url; email: password
     "settings": {"enabled": true, "server": "https://ntfy.sh", "topic": "sentry-...", "priority": "high"},
     "last": { /* delivery result of the latest attempt, test or real, or null */ },
     "last_ok": 1795000000.0},                  // last successful delivery (unix s) or null
    {"id": "webhook", "label": "Webhook", "url_hint": "https://hooks.example.com/…",
     "settings": {"enabled": false, "format": "json"}, "...": "..."},
    {"id": "email", "label": "Email",
     "settings": {"enabled": false, "host": "", "port": 587, "security": "starttls", "username": "",
                  "from": "", "to": []}, "...": "..."}
  ],
  "cooldown": {"last_sent": null, "suppressed": 0},   // events held back since the last alert
  "summary": {"channels": ["ntfy"], "failing": []},   // as in the status object
  "suggested_base_urls": ["http://100.64.0.1:8450/"]  // non-loopback addresses it listens on
}
```

Channel settings fields (for `POST /api/alerts/channels/{kind}`):

| Channel | Fields |
|---|---|
| `ntfy` | `enabled`, `server` (default `https://ntfy.sh`), `topic` (letters, digits, `-`, `_`, up to 64), `token` (secret; Bearer, or `user:password` for Basic), `priority` (`min` / `low` / `default` / `high` / `urgent`) |
| `webhook` | `enabled`, `url` (secret), `format` (`json` / `slack` / `discord`) |
| `email` | `enabled`, `host`, `port`, `security` (`starttls` / `ssl` / `none`), `username`, `password` (secret), `from`, `to` (list or comma-separated string, up to 10) |

### Delivery result

```jsonc
{"ok": false, "attempts": 3, "error": "ntfy.sh answered HTTP 403: refused (forbidden; check the token or topic)",
 "ms": 10412, "t": 1795000000.0, "channel": "ntfy",
 "kind": "open", "event": 12}   // kind: open | close | test | blind | watching | restart
// error names the server as host[:port] only; URLs and response bodies are never included
```

## Onboarding object

```jsonc
{"status": "active",      // pending | active | done | skipped | existing (an install from before 0.3.0)
 "step": "sensor",        // one of "steps"
 "steps": ["intro", "sensor", "learning", "walk", "alerts", "arm"],
 "show": true,            // the dashboard shows the setup guide
 "alerts_prompt": false}  // show the small "Set up phone alerts" prompt instead
```

## Event stream: `/api/stream`

`text/event-stream`. On connect the server sends `retry: 3000` and a `status` event, then:

| SSE `event:` | `data:` | When |
|---|---|---|
| `tick` | `{"t", "state", "score", "threshold", "sources": {"<id>": score}}` | every second |
| `status` | status object | on any change of state, armed, sensitivity, source online/health, learning progress (every ~3 s while learning), walk test start/stop, and after every control request |
| `motion` | event object | an event opened, changed (peak or sources, at most once a second, and every 5 s while the score stays above the off level) or closed |
| `alert` | event object | an alert was dispatched to the external sinks for this event |
| `walktest` | walk test object | a walk test finished |
| `alerts` | `{"result": delivery result or null, "summary"}` | after every delivery attempt and channel change |

A comment line `: keepalive` is sent after 15 s without other messages. Slow clients that fall
1000 messages behind lose messages rather than stalling the engine. The stream requires the same
access as every other `/api/*` endpoint; browsers send the cookie automatically.

## Webhook payload

The webhook channel `POST`s (format `json`; 0.2.0 sent only `kind`, `event`, `status`):

```jsonc
{"source": "wifi-sentry", "kind": "open",         // open | close | test | blind | watching | restart
 "title": "Motion detected at home", "message": "...",
 "time": "2026-11-23T09:22:45-05:00",              // ISO 8601, the Mac's local time
 "url": null,                                      // dashboard link if [alerts] base_url is set
 "suppressed": 0,                                  // events held back by the cooldown since the last alert
 "event": { "...": "event object (null for a test)" },
 "status": {"state": "motion", "armed": true, "score": 7.2, "threshold": 3.0, "sensitivity": "medium"}}
```

`kind` is `close` for the end-of-event message (only with `notify_on_close = true`); `blind`
("can't sense"), `watching` ("watching again") and `restart` ("was not running") report times when
it wasn't watching while armed (`event` is null for them and for `test`). Formats
`slack` and `discord` send `{"text": ...}` and `{"content": ...}` instead. Any 2xx answer counts as
delivered.
