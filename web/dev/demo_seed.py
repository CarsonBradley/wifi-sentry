#!/usr/bin/env python3
"""Fill a TEST instance's data directory with a simulated day, for README screenshots.

Writes ~27 hours of plausible activity history, a handful of motion events (some labelled), one
"no usable sensor signal" gap and a short engine restart into <data-dir>/sentry.db, relative to now.
Everything is made up; nothing comes from a real home. Stop the test engine first (it holds the
database), run this, then start it again: it picks up the history and, if the data directory has a
learned baseline less than a day old, goes straight to watching.

    python3 web/dev/demo_seed.py --data-dir /tmp/ws-demo/data
    python3 web/dev/demo_seed.py --write-config /tmp/ws-demo/test.toml --control-dir /tmp/ws-demo

--write-config writes a config for such a test instance (this tree's web/, the simulated sampler from
demo_sampler.py with its control files in --control-dir, alert targets allowed on 127.0.0.1 for the
local stubs in flows.py, ESP32 datagrams on 127.0.0.1:5577), then exits:
    ./run.sh --config /tmp/ws-demo/test.toml --port 8466 --hosts loopback --data-dir /tmp/ws-demo/data --learning-s 30

Refuses a data directory with more than a few events that it didn't seed itself (so it can't touch a\nreal one).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from pathlib import Path

THR = 3.0
# (minutes ago, seconds long, peak as a multiple of the trigger, armed, label, note)
EVENTS = [
    (64, 31, 4.1, True, None, ""),
    (162, 9, 1.35, True, "false", "Robot vacuum"),
    (236, 18, 2.2, True, None, ""),
    (301, 46, 2.9, False, None, ""),
    (780, 6, 1.2, True, "false", ""),
    (968, 25, 2.4, False, None, ""),
    (1142, 72, 5.3, False, "real", "Me, back from the shop"),
    (1325, 12, 1.9, False, None, ""),
    (1540, 38, 3.4, False, None, ""),
]
GAPS = [(545, 533, "source_offline"), (1206, 1203, "engine_down")]   # (from, to) minutes ago


def write_config(out: Path, ctl: Path) -> int:
    root = Path(__file__).resolve().parents[2]
    py = root / "engine" / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)   # e.g. a worktree without its own venv: run this with the engine's Python
    cmd = [str(py), str(root / "web" / "dev" / "demo_sampler.py"), "--control", str(ctl / "moving"),
           "--mode-file", str(ctl / "mode"), "--amp", "1.0"]
    out.write_text("# Test instance for dashboard screenshots (web/dev/demo_seed.py). Not for real use.\n"
                   "[server]\nweb_dir = %s\n\n[mac]\ncommand = %s\n\n"
                   "[esp32]\nenabled = true\nbind = \"127.0.0.1\"\nport = 5577\nkey = \"\"\n\n"
                   "[alerts]\nallow_loopback_targets = true\n" % (json.dumps(str(root / "web")), json.dumps(cmd)))
    ctl.mkdir(parents=True, exist_ok=True)
    print("wrote", out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir")
    ap.add_argument("--write-config", metavar="TOML")
    ap.add_argument("--control-dir", help="with --write-config: where demo_sampler.py looks for its control files")
    ap.add_argument("--hours", type=float, default=27.0)
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()
    if a.write_config:
        return write_config(Path(a.write_config), Path(a.control_dir or Path(a.write_config).parent))
    if not a.data_dir:
        ap.error("--data-dir is required")
    db_path = Path(a.data_dir) / "sentry.db"
    if not db_path.exists():
        sys.exit("no sentry.db there: start the test engine once first (it creates the schema and learns a baseline)")
    db = sqlite3.connect(str(db_path), isolation_level=None)
    n = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    seeded = db.execute("SELECT 1 FROM kv WHERE k='demo_seed'").fetchone()
    if n > 5 and not seeded:
        sys.exit(f"{n} events already recorded: this looks like a real data directory, refusing")
    rng = random.Random(a.seed)
    now = time.time()
    t0 = now - a.hours * 3600
    gaps = [(now - f * 60, now - t * 60, r) for f, t, r in GAPS]
    evs = [(now - m * 60, now - m * 60 + d, k * THR, armed, label, note) for m, d, k, armed, label, note in EVENTS]

    def score(t: float) -> float:
        for s, e, peak, *_ in evs:
            if s - 2 <= t <= e + 2:
                x = (t - s) / max(1.0, e - s)
                env = math.sin(math.pi * min(1.0, max(0.0, x))) ** 0.6
                return max(0.2, peak * env * (0.75 + 0.25 * rng.random()))
        base = abs(rng.gauss(0.22, 0.11))
        if rng.random() < 0.004:
            base += rng.uniform(0.4, 1.1)
        return round(base, 4)

    db.execute("BEGIN")
    db.execute("DELETE FROM events")
    db.execute("DELETE FROM history")
    db.execute("DELETE FROM history_src")
    db.execute("DELETE FROM gaps")
    rows, src = [], []
    t = t0
    while t < now - 2:
        if not any(s <= t < e for s, e, _ in gaps):
            sc = score(t)
            rows.append((round(t, 3), sc, THR, "motion" if sc > THR else "clear"))
            src.append((round(t, 3), "mac", sc))
        t += 2.0
    db.executemany("INSERT OR REPLACE INTO history(t, score, threshold, state) VALUES (?,?,?,?)", rows)
    db.executemany("INSERT OR REPLACE INTO history_src(t, src, score) VALUES (?,?,?)", src)
    for s, e, r in gaps:
        db.execute("INSERT INTO gaps(start, end, reason) VALUES (?,?,?)", (round(s, 3), round(e, 3), r))
    for s, e, peak, armed, label, note in sorted(evs):
        db.execute("INSERT INTO events(start, end, peak_score, threshold, sources, armed, label, note, walktest) "
                   "VALUES (?,?,?,?,?,?,?,?,0)", (round(s, 3), round(e, 3), round(peak, 3), THR, json.dumps(["mac"]),
                                                   int(armed), label, note))
    for k, v in (("onboarding", {"status": "done", "step": "intro", "alerts_prompt_dismissed": True}),
                 # a heartbeat just ahead of now, so starting the engine right after records no restart gap
                 ("demo_seed", True), ("sensitivity", "medium"), ("heartbeat", now + 20)):
        db.execute("INSERT INTO kv(k, v, updated) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                   "updated=excluded.updated", (k, json.dumps(v), now))
    # forget simulated ESP32 boards from earlier runs (the Mac's learned baseline stays, with its age)
    r = db.execute("SELECT v FROM kv WHERE k='baselines'").fetchone()
    if r:
        bl = {k: v for k, v in json.loads(r[0]).items() if k == "mac"}
        db.execute("UPDATE kv SET v=? WHERE k='baselines'", (json.dumps(bl),))
    db.execute("COMMIT")
    print(f"seeded {len(rows)} history rows, {len(evs)} events, {len(gaps)} gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
