# Contributing to wifi-sentry

Thanks for looking. This is an early project tested on a single Mac, so reports from other
machines are as valuable as code.

## The most useful thing: a compatibility report

1. Clone and run `./install.sh` (it ends with a 2-second probe; nothing needs to keep running).
2. Run:

   ```sh
   ./wifi-sentry probe
   ```

   Example output from the one tested Mac:

   ```text
   This Mac: Mac17,16 (arm64), macOS 27.0.1, Wi-Fi interface en1, link up
   Full support: 2 per-antenna signal readings (the design wifi-sentry was tuned on).
     per-antenna signal: ok (2 antenna value(s); 20/20 reads valid, 8 changes in 2 s)
     overall signal (rssi): yes;  noise: yes;  MCS: yes
     driver statistics (IOReport): 13 of 13 channels
   ```

3. If you can, also start it, let it learn for 10 minutes, run the walk test from the dashboard
   (Settings → Walk test), and note the result.
4. [Open a compatibility report](https://github.com/CarsonBradley/wifi-sentry/issues/new?template=compatibility.yml)
   with the probe output, your Mac model (Apple menu → About This Mac), macOS version, whether it's
   a laptop, and the walk-test result. Or add a row to [docs/compatibility.md](docs/compatibility.md)
   in a pull request.

For a bug, include `./wifi-sentry doctor` instead. **Check its output before pasting**: it prints
your local paths (which contain your user name) and the addresses the dashboard listens on (your
Tailscale or LAN IP). Replace those with placeholders. Never paste the contents of
`data/access-token` or `config.local.toml`.

Recordings in `data/raw/` help with false-alarm reports (they contain only signal strengths,
counters and timestamps, but timestamps of motion say when someone was home, so share them
knowingly).

## Development setup

Requirements: macOS, Xcode Command Line Tools, [uv](https://docs.astral.sh/uv/).

```sh
./install.sh              # builds sensor/wifisense, creates engine/.venv (uv sync --locked)
scripts/test.sh           # engine test suite
```

### Tests without Wi-Fi

The suite needs no Wi-Fi, no network beyond 127.0.0.1, and never sends alerts. One test, marked
`hardware`, probes the real Wi-Fi for about a second; it skips without a built sampler, off macOS,
or with:

```sh
WIFI_SENTRY_NO_HARDWARE=1 scripts/test.sh
```

That's what CI runs (`.github/workflows/ci.yml`). Extra arguments go to pytest, e.g.
`scripts/test.sh -k auth -x`.

The firmware's JSON encoder has its own host test (needs only `cc` and `python3`):

```sh
sh esp32/test/run_host_test.sh
```

### Running a second engine without touching the real one

```sh
./run.sh --config config.toml --port 8452 --hosts loopback --data-dir /tmp/ws-test --learning-s 60 --fake-mac
```

`--config config.toml` keeps your `config.local.toml` (alert channels, exposure) out of the test
instance; its token is in `/tmp/ws-test/access-token`
(`curl -H "Authorization: Bearer $(cat /tmp/ws-test/access-token)" http://127.0.0.1:8452/api/status`,
or open `http://127.0.0.1:8452/?token=...` in a browser). `--fake-mac` replays the bundled 10-minute
idle recording instead of the real sampler. Modes
`single`, `multi`, `aggregate`, `off`, `disconnected` and `none` simulate Macs that expose fewer
signals, or none. A fake ESP32 board: `python3 esp32/tools/fake_esp32.py --port 5567 --burst-every 40 --burst-len 8`
together with `--esp32-port 5567` and `[esp32] enabled = true` in the test instance's `--config`.

### Replaying recordings

```sh
scripts/replay.sh engine/tests/fixtures/mac-idle-20261123-001939.jsonl.gz --learning-s 180 --quiet
scripts/replay.sh data/raw/raw-YYYYMMDD-HH.jsonl.gz --learning-s 180     # your own recordings
```

Replay runs the exact live detection code and reports the events each sensitivity would have
produced. `--inject START:END,... --intensity A` adds simulated walking (see
`engine/src/sentry/sim.py`; it is a model, not measured walking). If you change the detector,
show its effect on the bundled night fixtures and on injected motion.

### Dashboard development

`web/` is plain HTML, CSS and JavaScript with no build step and no external requests.
`python3 web/dev/mock_server.py` serves it on 127.0.0.1:8451 with simulated data and scenarios
(`--scenario idle | learning | motion | offline | esp32 | flaky`); `web/dev/check.py` and
`web/dev/shoot.py` drive it in a headless browser (Playwright, see their docstrings).
`web/dev/flows.py` runs the alerts, arming and setup flows against a real second engine, and
`web/dev/shoot_engine.py` screenshots every state (login, motion, learning, reduced and degraded
sensing, Wi-Fi off, disconnected, ESP32 health) on one, with a simulated day from
`web/dev/demo_seed.py` and simulated motion from `web/dev/demo_sampler.py`; it also makes the
README images (`--readme`).

## Code layout

| Path | What |
|---|---|
| `sensor/` | `wifisense`, the Swift sampler (CoreWLAN + private CoreWiFi / IOReport), `build.sh`. [README](sensor/README.md) documents every field. |
| `engine/src/sentry/` | Python engine: `features.py` (signal to feature), `baseline.py`, `detector.py` (score, events, walk test), `sources.py` (sampler process, ESP32 UDP), `app.py` (HTTP API, SSE), `auth.py`, `storage.py` (SQLite, raw recorder), `arming.py` (exit delay, schedule), `alertcfg.py` (alert channels set up on the dashboard), `sinks.py` (alert delivery), `config.py`, `net.py`, `cli.py` (behind `./wifi-sentry`), `replay.py`, `sim.py`, `fake_sampler.py` |
| `engine/tests/` | pytest suite; `fixtures/` holds real recordings from the tested Mac (signal strengths and counters only) |
| `web/` | dashboard; `web/dev/` mock server and browser checks |
| `esp32/` | optional CSI firmware (ESP-IDF), simulator and tools |
| `install.sh`, `wifi-sentry`, `run.sh`, `scripts/` | installer, CLI, foreground runner, test / replay helpers |
| `config.toml` | committed defaults; `config.local.toml` is yours (git-ignored) |
| `docs/` | [how it works](docs/how-it-works.md), [configuration](docs/configuration.md), [API](docs/api.md), [compatibility](docs/compatibility.md) |

## Pull requests

- Keep changes focused, and add or update tests. `WIFI_SENTRY_NO_HARDWARE=1 scripts/test.sh` must
  pass.
- Don't commit anything machine-specific: addresses, `/Users/...` paths, tokens, `config.local.toml`,
  `data/`, `esp32/sdkconfig` (it holds your Wi-Fi password). `engine/tests/test_repo_hygiene.py`
  checks for the usual suspects; use the placeholders it allows (for example `192.168.1.x`,
  `100.64.x.x`, `192.0.2.x`).
- Be explicit about evidence. If something was measured, say on what; if it was simulated or only
  compiled, say that. The docs try hard not to claim more than has been observed.
- The version lives in one place: `version` in `engine/pyproject.toml`. Add a line under an
  `## Unreleased` heading at the top of [CHANGELOG.md](CHANGELOG.md) (add the heading if it isn't
  there yet).

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
