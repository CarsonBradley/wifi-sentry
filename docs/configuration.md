# Configuration reference

wifi-sentry reads three layers, each overriding the one before key by key:

1. built-in defaults (`engine/src/sentry/config.py`, `DEFAULTS`);
2. **`config.toml`**: the committed, generic defaults. Don't edit it for your own setup;
3. **`config.local.toml`**: your machine's settings and secrets. Git-ignored, created by
   `./install.sh` from `config.local.example.toml`, kept by `./wifi-sentry update`.

After changing either file run `./wifi-sentry restart`. `./wifi-sentry doctor` lists the files in
effect and any configuration notes; a syntax error stops the engine with the file name and line.

Times are in seconds. "Score" means the normalised motion score (idle sits around 0 to 1; see
[how-it-works.md](how-it-works.md)). Some settings can also be changed from the dashboard
(sensitivity, arming, exit delay and schedule in `data/sentry.db`; alert channels in
`data/alerts.json`); those take precedence after the first change.

Every key below was checked against `DEFAULTS` in `engine/src/sentry/config.py` and the code that
reads it (version 0.3.0; changes between versions are in the [changelog](../CHANGELOG.md)).

## `[server]`: the dashboard and API

| Key | Default | Meaning |
|---|---|---|
| `port` | `8450` | HTTP port. |
| `expose` | `[]` | Where to listen **in addition to** 127.0.0.1, which is always bound. Entries: `"tailscale"` (this Mac's Tailscale 100.x address, found with the `tailscale` CLI; retried every 30 s until Tailscale is up), `"lan"` (the Wi-Fi interface's IPv4: anyone on your network can reach the login page), or a literal IP address of this Mac. Wildcards (`"0.0.0.0"`, `"::"`) are refused. Everything beyond 127.0.0.1 requires the access token. |
| `web_dir` | `"web"` | Directory of the static dashboard, relative to the project root. |
| `hosts` | (unset) | **Deprecated** (0.1.x). If present it is used as the complete bind list and `doctor` shows a note. Use `expose` instead. |

## `[auth]`: access token

| Key | Default | Meaning |
|---|---|---|
| `loopback_exempt` | `false` | `true` lets requests from this Mac itself (loopback address, loopback `Host` header, no proxy headers) skip the token, as in 0.2. Only on a Mac nobody else uses: other accounts, programs and browser extensions reach 127.0.0.1 too, and port forwards without proxy headers (`ssh -L`, `socat`, `tailscale serve --tcp`, Docker) make remote users look local. `doctor` warns when it is on. |
| `token_file` | `"access-token"` | Token file, relative to the data directory. Created with mode 600 if missing. Delete it and restart to rotate the token (logs every browser out). |
| `max_failures` | `10` | Wrong tokens allowed per client address within `failure_window_s`; after that further wrong tokens get HTTP 429. A correct token is always accepted; requests from this Mac are never counted. |
| `failure_window_s` | `300` | Window for `max_failures`. |
| `cookie_days` | `30` | Lifetime of a login session, enforced on the server (sessions are kept in `data/sessions.json`). (Not listed in `config.toml`; set it in `config.local.toml` if needed.) |

## `[paths]`

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | `"data"` | Database, logs, raw recordings, walk tests, token. Relative paths are resolved against the project root. Only one engine can run per data directory. |

## `[mac]`: the Mac's own Wi-Fi sensor

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Spawn the sampler. |
| `command` | `["sensor/wifisense", "--hz", "10"]` | Sampler command line. Useful additions: `"--keepalive", "5"` (ping the router 5×/s, for a link with very little traffic). `doctor` flags a non-default command. |
| `restart_min_s` | `1.0` | Restart back-off after the sampler exits: starts here, doubles each time... |
| `restart_max_s` | `60.0` | ...up to this. |
| `weight` | `1.0` | Multiplier on this source's score in the fused score (fused = highest weighted source score). |

## `[esp32]`: optional CSI boards

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Listen for ESP32 boards. **Without a `key` the UDP port is unauthenticated**: anyone who can reach it can send fake sensor data. |
| `bind` | `"lan"` | `"lan"` (this Mac's Wi-Fi address), `"127.0.0.1"`, an explicit address, or `"0.0.0.0"` (every interface; accepted only when written explicitly, logged as a warning). |
| `port` | `5566` | UDP port. |
| `weight` | `1.0` | Multiplier on ESP32 scores in the fused score. |
| `key` | `""` | Shared key the boards sign with (menuconfig *Shared key*, same string). When set, unsigned or wrongly signed datagrams are dropped and replayed ones rejected; see [esp32/README.md](../esp32/README.md#6-wire-format-for-the-engine-author). Put it in `config.local.toml`. `""` accepts unsigned boards. |

## `[sources]`: data health

| Key | Default | Meaning |
|---|---|---|
| `offline_after_s` | `5.0` | A source with no data for this long is offline. |
| `gap_reset_s` | `1.5` | A gap this long between two samples clears that source's feature window. |
| `channel_settle_s` | `5.0` | After a Wi-Fi channel or bandwidth change, ignore the source's score for this long. |
| `mac_frozen_s` | `15.0` | Per-antenna readings unchanged this long **and** the packet counter stalled = the Mac sensor is blind ("frozen", shown as not watching). Without a packet counter in the stream, unchanged readings alone count. |
| `mac_stuck_s` | `600.0` | Readings unchanged this long despite traffic are reported as "stuck". |

## `[detect]`: the score

| Key | Default | Meaning |
|---|---|---|
| `mac_window_s` | `3.0` | Window of per-antenna readings per score. |
| `esp32_window_s` | `2.0` | Window of CSI amplitudes per ESP32 score. |
| `mac_common_weight` | `1.0` | Weight `w` of the common-mode part: `r = V(d) + w·V(c)`. Lower trusts only the difference between antennas (more immune to gain changes, blind to plain shadowing). |
| `mac_step_robust` | `true` | Use the variance left after removing the best single level step, so a lone step doesn't open an event. |
| `mac_pulse_reject` | `true` | Drop short common-mode "boxes" (both antennas up together, back down quickly) before the variance. See [how-it-works.md §5](how-it-works.md#5-the-first-night-a-radio-artifact-that-looked-like-motion). |
| `mac_pulse_max_s` | `2.0` | Longest box that is dropped. |
| `mac_pulse_min_db` | `2.0` | Smallest rise that counts as a box. |
| `mac_pulse_max_diff_db` | `1.0` | Antennas must stay within this of each other during the box. |
| `mad_k` | `3.0` | `scale = max(mad_k · 1.4826 · MAD, rel_floor · median, abs_floor)`. |
| `mac_abs_floor` | `0.15` | Smallest scale for the Mac score (dB²). |
| `mac_rel_floor` | `0.0` | Scale floor relative to the median, Mac. |
| `esp32_abs_floor` | `0.002` | Smallest scale for ESP32 scores. |
| `esp32_rel_floor` | `0.25` | Scale floor relative to the median, ESP32. |

### `[detect.thresholds]`

| Key | Default | Meaning |
|---|---|---|
| `low` | `5.0` | Trigger level at sensitivity Low. |
| `medium` | `3.0` | ...Medium. |
| `high` | `2.0` | ...High. All three must be > 0. |

### `[detect.events]`

| Key | Default | Meaning |
|---|---|---|
| `sensitivity` | `"medium"` | Level at first start. Changing it on the dashboard persists and wins afterwards. |
| `off_ratio` | `0.6` | Hysteresis: an event (or one building up) survives dips down to `off_ratio × trigger`. |
| `min_duration_s` | `2.0` | Time at or above the trigger before an event opens. 3.0 removes more brief false events but adds about 1 s latency and misses more brief or weak motion. |
| `end_timeout_s` | `10.0` | An event closes this long after the score drops below the off level. |
| `merge_gap_s` | `30.0` | A new burst this soon after an event's end re-opens that event. |

### `[detect.aux]`: interference discount (Mac only)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Discount the Mac score when noise / channel busy time jump above their own baseline. |
| `z0` | `3.0` | Robust z-score above which the discount starts. |
| `k` | `0.25` | `factor = 1 / (1 + k · max(0, weighted_z − z0))`. |
| `min_factor` | `0.5` | Never discount below this. |
| `w_noise` | `1.0` | Weight of the noise floor. |
| `w_cca` | `0.7` | Weight of channel busy time. |
| `w_cca_intf` | `1.0` | Weight of non-Wi-Fi interference busy time. |
| `w_retrans` | `0.0` | Weight of the retry ratio (off: a body moving also raises retries). |

## `[baseline]`: learning the still room

| Key | Default | Meaning |
|---|---|---|
| `learning_s` | `600` | Initial learning period (also after "Relearn baseline"). |
| `min_learning_ticks` | `30` | Seconds of learning before a provisional score is shown. |
| `window_s` | `3600` | Rolling window of accepted seconds for median / MAD. |
| `recompute_every_s` | `10` | How often median / MAD are recomputed. |
| `holdoff_s` | `60` | Nothing is learned until this long after an event ended. |
| `gate_ratio` | `0.5` | A second is only learned if its score is below `gate_ratio × trigger`. |
| `absorb_after_s` | `900` | A source rejected continuously this long... |
| `absorb_window_s` | `300` | ...whose feature over this window... |
| `absorb_min_frac` | `0.9` | ...was elevated in at least this fraction of seconds... |
| `absorb_max_cv` | `0.5` | ...and stationary (robust coefficient of variation at most this) has its baseline rebuilt from that window (a fan switched on). |
| `force_absorb_after_s` | `0` | Absorb after this long regardless of stationarity (0 = never). |
| `save_every_s` | `60` | Save baselines to the database this often (and on stop). |
| `max_age_s` | `86400` | Reuse a saved baseline at start if younger than this. |

## `[arming]`

| Key | Default | Meaning |
|---|---|---|
| `delay_s` | `60` | Exit delay when arming from the dashboard switch: motion in this time is logged but not alerted. The dashboard can change it (Settings → Arming), and set a weekly schedule; both are stored in `data/sentry.db`, and once arming settings have been saved there this key no longer applies. `POST /api/arm` without `delay_s` arms at once. |

## `[alerts]`: where armed events go

Only events that open **while armed**, and not during a walk test, are sent. The dashboard always
shows every event. All channels are off by default.

The easiest way to set channels up is the dashboard's **Phone alerts** panel. It stores its settings
in `data/alerts.json` (mode 600) and, **per channel**, a channel saved there replaces the
`[alerts.<channel>]` section below; "Remove" on the dashboard returns that channel to the file.
Channels never touched on the dashboard keep using the file. If you configure by hand, put
credentials in `config.local.toml`.

| Key | Default | Meaning |
|---|---|---|
| `cooldown_s` | `300` | Minimum time between two motion alerts (0.2.0: 60), kept separately for events the Mac's own sensor saw and events only ESP32 boards saw. Events in between are not sent but counted in the next alert. |
| `notify_on_close` | `false` | Also send "Motion ended" for events that were alerted. |
| `base_url` | `""` | Dashboard link to include in alerts (and the ntfy tap target), e.g. your Tailscale address. Empty = no link. |
| `not_watching_s` | `120` | While armed and not watching this long (sensor offline or blind, sampler crash-looping, learning): one "can't sense" alert, then "watching again" when it recovers. A down time (engine stopped, Mac asleep) at least this long is reported as "was not running" afterwards. `0` = off. |
| `not_watching_cooldown_s` | `1800` | At most one "can't sense" / "was not running" alert per this long (separate from `cooldown_s`). |
| `allow_loopback_targets` | `false` | Allow alert targets on this Mac's loopback address (a self-hosted ntfy on the same Mac). Off, a webhook can't be aimed at other local services. Only in the config file, never through the API. |

### `[alerts.ntfy]`: push to the [ntfy](https://ntfy.sh) app

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `url` | `""` | Topic URL, e.g. `https://ntfy.sh/a-long-unguessable-topic`. On the public ntfy.sh server anyone who knows the topic can read it, so make it long and random, or use your own server. |
| `priority` | `"high"` | ntfy priority for "motion detected" ("motion ended" is sent at default priority). |
| `token` | `""` | Access token if your ntfy server needs one (sent as Bearer), or `user:password` (sent as Basic auth). |

### `[alerts.webhook]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `url` | `""` | Receives a `POST` for every alert. |
| `format` | `"json"` | `"json"`: `{"source": "wifi-sentry", "kind": "open"\|"close"\|"test", "title", "message", "time", "url", "suppressed", "event": {...}, "status": {"state", "armed", "score", "threshold", "sensitivity"}}` (the event object is in [api.md](api.md#event-object)); `"slack"`: `{"text"}`; `"discord"`: `{"content"}`. |

### `[alerts.email]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `host` | `""` | SMTP server. |
| `port` | `587` | |
| `starttls` | `true` | Use STARTTLS. Alternatively `security = "starttls"`, `"ssl"` or `"none"`; with `starttls = false` and no `security`, port 465 means SSL, anything else plain. |
| `username` | `""` | Login is skipped when empty. |
| `password` | `""` | Use an app password where your provider supports one. |
| `from` | `""` | Sender address. |
| `to` | `[]` | Recipient list (a single string also works). |

Delivery failures are logged in `data/logs/engine.log` and listed under `alerts` in `/api/debug`;
they never stop detection.

## `[storage]`

| Key | Default | Meaning |
|---|---|---|
| `history_keep_days` | `7` | Score history and coverage gaps older than this are deleted. Events are kept. |

## `[record]`: raw recordings for replay and tuning

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Write raw sensor samples to `data/raw/` (hourly JSONL, gzipped after the hour). |
| `include_esp32` | `false` | Also record ESP32 frames (about 105 MB/hour raw per board). Off by default: it is a lot of data, and whatever reaches the UDP port would be written to disk. |
| `keep_hours` | `24` | Delete recordings older than this... |
| `max_bytes` | `2_000_000_000` | ...or beyond this total size, whichever comes first. Checked on every write: the oldest files are deleted to make room, and if the current hour alone reaches it, samples are dropped until the next hour. |

The Mac alone writes about 16 MB/hour before compression, roughly 10 to 40 MB/day once gzipped.

## `[walktest]`

| Key | Default | Meaning |
|---|---|---|
| `max_duration_s` | `180` | A walk test stops by itself after this long. |
| `reliable_fraction` | `0.2` | A level "reliably" caught the walk if it would have fired and at least this fraction of the walk's seconds were above its trigger. The recommendation is the least sensitive reliable level. |

## Engine command-line flags

`./wifi-sentry run [FLAGS]` or `./run.sh [FLAGS]` (foreground) accept, from `python -m sentry.main --help`:

| Flag | Overrides |
|---|---|
| `--config FILE` | Use this file instead of `config.toml` + `config.local.toml`. |
| `--log-file PATH` | Rotating log file (default: the terminal). |
| `--port N` | `server.port` |
| `--hosts LIST` | Replaces the whole bind list, comma separated: `loopback`, `tailscale`, `lan` or addresses. |
| `--esp32-port N` | `esp32.port` |
| `--data-dir DIR` | `paths.data_dir` |
| `--no-mac` | Don't spawn the sampler. |
| `--fake-mac [MODE]` | Replay the bundled idle recording instead of the real sampler. `MODE`: `dual` (default), `single`, `multi`, `aggregate`, `off`, `disconnected`, `none`. |
| `--learning-s S` | `baseline.learning_s` |
| `-v`, `--verbose` | Debug logging. |

A private test instance that can't touch a running one:

```sh
./run.sh --config config.toml --port 8452 --hosts loopback --data-dir /tmp/ws-test --learning-s 60 --fake-mac
```
