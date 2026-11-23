"""Stand-in for sensor/wifisense: replays recorded wifisense lines in real time with current timestamps.

For tests, CI (no Wi-Fi), a second engine next to a live one, and simulating Macs that expose fewer
signals than the one the detector was built on:

  python -m sentry.fake_sampler                      # 2 antennas, the 10-min idle capture, looped
  python -m sentry.fake_sampler --mode single        # one antenna value only
  python -m sentry.fake_sampler --mode multi         # 3 antennas (third one synthesised)
  python -m sentry.fake_sampler --mode aggregate     # no per-antenna API: overall rssi only
  python -m sentry.fake_sampler --mode off           # Wi-Fi turned off (also: disconnected, none)

The engine runs it with `./run.sh --fake-mac MODE` (or `[mac] command`). Output format and the
leading {"type":"caps"} line are the same as wifisense's."""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
from pathlib import Path

from . import __version__

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "mac-idle-20261123-001939.jsonl.gz"
MODES = ("dual", "single", "multi", "aggregate", "off", "disconnected", "none")


def caps_for(mode: str, t: float) -> dict:
    link = {"off": "off", "disconnected": "disconnected", "none": "none"}.get(mode, "up")
    chains = {"dual": 2, "single": 1, "multi": 3}.get(mode, 0)
    pa = "unknown" if link != "up" else ("ok" if chains else "missing")
    return {"type": "caps", "v": 1, "t": round(t, 3), "src": "mac", "sampler": f"fake-{__version__}",
            "os": "fake", "arch": "fake", "model": "fake", "iface": None if mode == "none" else "en9",
            "link": link, "per_antenna": pa, "chains": chains, "ioreport": False, "mcs": False}


def transform(row: dict, mode: str, rng: random.Random) -> dict:
    r = dict(row)
    r.pop("type", None)
    if mode in ("off", "disconnected", "none"):
        for k in list(r):
            if k not in ("t", "src", "rx_pkts", "tx_pkts"):
                r[k] = None
        if mode == "none":
            r["rx_pkts"] = r["tx_pkts"] = None
        return r
    if mode == "single":
        r.pop("rssi_c1", None)
    elif mode == "aggregate":
        r.pop("rssi_c0", None)
        r.pop("rssi_c1", None)
    elif mode == "multi" and r.get("rssi_c0") is not None and r.get("rssi_c1") is not None:
        r["rssi_c2"] = int(round((r["rssi_c0"] + r["rssi_c1"]) / 2 + rng.uniform(-0.6, 0.6))) - 2
    return r


def read_rows(path: Path) -> list[dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return [r for r in rows if r.get("type") != "caps" and "csi" not in r]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=MODES, default="dual")
    ap.add_argument("--file", type=Path, default=FIXTURE, help="wifisense JSONL(.gz) to replay (looped)")
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    ap.add_argument("--fast", action="store_true", help="don't sleep (timestamps still advance at --hz)")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    rows = read_rows(a.file)
    if not rows:
        print("fake_sampler: no rows in " + str(a.file), file=sys.stderr)
        return 1
    rng = random.Random(a.seed)
    period = 1.0 / a.hz
    t0 = time.time()
    out = sys.stdout
    try:
        out.write(json.dumps(caps_for(a.mode, t0), separators=(",", ":")) + "\n")
        i = 0
        while a.duration is None or i * period < a.duration:
            t = t0 + i * period
            if not a.fast:
                delay = t - time.time()
                if delay > 0:
                    time.sleep(delay)
            r = transform(rows[i % len(rows)], a.mode, rng)
            r["t"] = round(t, 3)
            out.write(json.dumps(r, separators=(",", ":")) + "\n")
            out.flush()
            i += 1
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
