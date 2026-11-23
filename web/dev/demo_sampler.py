#!/usr/bin/env python3
"""Simulated wifisense for dashboard screenshots: the recorded idle capture, plus motion on demand.

Replays the same still-room capture as `./run.sh --fake-mac` and, while the control file exists,
adds an independent random walk of a few dB to each antenna, which the detector scores like a person
walking past. Nothing here is real data. A test instance runs it through its config:

    [mac]
    command = ["<engine python>", "web/dev/demo_sampler.py", "--control", "/tmp/ws-demo/moving"]

    touch /tmp/ws-demo/moving    # someone starts moving
    rm /tmp/ws-demo/moving       # still again

With --mode-file, the file's content (a fake-sampler mode: single, aggregate, off, ...) replaces
--mode; when it changes the sampler exits, and the engine restarts it with the new capabilities.

`--mode` takes the fake sampler's modes (single, aggregate, off, ...) to show reduced or no sensing.
Needs the engine's Python (it imports sentry.fake_sampler).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

from sentry.fake_sampler import FIXTURE, MODES, caps_for, read_rows, transform


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--control", required=True, help="motion is simulated while this file exists")
    ap.add_argument("--mode", choices=MODES, default="dual")
    ap.add_argument("--mode-file", help="read the mode from this file (if it exists); exit when it changes")
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--amp", type=float, default=3.0, help="size of the simulated disturbance, dB")
    a = ap.parse_args()

    def file_mode():
        try:
            m = open(a.mode_file).read().strip() if a.mode_file else ""
        except OSError:
            m = ""
        return m if m in MODES else a.mode

    mode = file_mode()
    rows = read_rows(FIXTURE)
    rng = random.Random(7)
    period = 1.0 / a.hz
    t0 = time.time()
    walk = [0.0, 0.0, 0.0]
    out = sys.stdout
    try:
        out.write(json.dumps(caps_for(mode, t0), separators=(",", ":")) + "\n")
        i = 0
        while True:
            t = t0 + i * period
            delay = t - time.time()
            if delay > 0:
                time.sleep(delay)
            r = transform(rows[i % len(rows)], mode, rng)
            r["t"] = round(t, 3)
            if os.path.exists(a.control):
                for k, key in enumerate(("rssi_c0", "rssi_c1", "rssi")):
                    walk[k] = max(-2.5 * a.amp, min(2.5 * a.amp, walk[k] * 0.8 + rng.gauss(0, a.amp)))
                    if isinstance(r.get(key), (int, float)):
                        r[key] = int(round(r[key] + walk[k]))
            else:
                walk = [0.0, 0.0, 0.0]
            out.write(json.dumps(r, separators=(",", ":")) + "\n")
            out.flush()
            i += 1
            if a.mode_file and i % 10 == 0 and file_mode() != mode:
                return 0
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    sys.exit(main())
