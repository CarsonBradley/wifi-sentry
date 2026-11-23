"""Replay recorded samples (or the in-process simulator) through the exact live detection code.

Examples (from the project root):
  scripts/replay.sh engine/tests/fixtures/mac-idle-20261123-001939.jsonl.gz --learning-s 180
  scripts/replay.sh engine/tests/fixtures/mac-idle-20261123-001939.jsonl.gz --loop 2   # learn on pass 1, judge pass 2
  scripts/replay.sh engine/tests/fixtures/mac-idle-20261123-001939.jsonl.gz --inject 300:320,400:430 --intensity 2
  scripts/replay.sh engine/tests/fixtures/night-20261123-0300-0326.jsonl.gz --learning-s 180 --quiet
  scripts/replay.sh --sim --sim-duration 900 --burst-every 90 --burst-len 8 --nodes 2 --learning-s 60
  scripts/replay.sh capture.jsonl --truth truth.csv      # udp_dump --save capture + fake_esp32 --truth
  scripts/replay.sh data/raw/raw-YYYYMMDD-HH.jsonl.gz    # your engine's own raw recordings
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from pathlib import Path

import numpy as np

from .config import LEVELS, load_config
from .detector import Detector
from .sim import esp32_stream, inject_mac_motion, score_events, shift_rows, truth_intervals


def open_text(path: str | Path):
    p = str(path)
    return gzip.open(p, "rt") if p.endswith(".gz") else open(p, "r")


def read_lines(paths) -> list[dict]:
    rows = []
    for p in paths:
        with open_text(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def classify(rows: list[dict], esp_t0: float | None = None) -> list[tuple[float, str, str, dict]]:
    """-> sorted (t, src, kind, payload). ESP32 frames use rx_t if recorded, else t_us."""
    out = []
    mac_ts = [r["t"] for r in rows if "csi" not in r and isinstance(r.get("t"), (int, float))]
    base = esp_t0 if esp_t0 is not None else (min(mac_ts) if mac_ts else 0.0)
    esp_map: dict[str, list] = {}  # src -> [offset, last_tus, last_t]
    for r in rows:
        if "csi" in r:
            src = str(r.get("src", "esp32:?"))
            if isinstance(r.get("rx_t"), (int, float)):
                t = float(r["rx_t"])
            else:
                tus = r.get("t_us", 0)
                st = esp_map.get(src)
                if st is None:
                    st = esp_map[src] = [base - tus / 1e6, tus, None]
                elif tus < st[1]:  # reboot: continue the clock
                    st[0] = (st[2] or base) + 0.02 - tus / 1e6
                st[1] = tus
                t = st[0] + tus / 1e6
                st[2] = t
            out.append((t, src, "esp32", r))
        elif isinstance(r.get("t"), (int, float)):
            out.append((float(r["t"]), str(r.get("src", "mac")), "mac", r))
    out.sort(key=lambda x: x[0])
    return out


def run_replay(samples, cfg: dict, sensitivity: str | None = None, armed: bool = False,
               detector: Detector | None = None, tail_s: float | None = None, on_tick=None) -> dict:
    """Feed time-ordered samples through a Detector ticking at 1 Hz of data time."""
    det = detector or Detector(cfg)
    if sensitivity:
        det.set_sensitivity(sensitivity)
    det.set_armed(armed)
    events: dict[int, dict] = {}
    ticks = []
    next_tick = None

    def do_tick(tt):
        res = det.tick(tt)
        ticks.append((tt, res.score, res.state, dict(res.sources)))
        for kind, obj in res.emits:
            if kind == "event_open":
                events[obj["id"]] = dict(obj, opened_at=tt)
            elif kind in ("event_update", "event_close"):
                prev = events.get(obj["id"], {})
                events[obj["id"]] = dict(prev, **obj, opened_at=prev.get("opened_at", tt))
        if on_tick:
            on_tick(det, res)

    last_t = None
    for t, src, kind, payload in samples:
        if next_tick is None:
            next_tick = math.floor(t) + 1.0
        while t >= next_tick:
            do_tick(next_tick)
            next_tick += 1.0
        det.ingest(src, kind, payload, t)
        last_t = t
    if last_t is not None:
        tail = tail_s if tail_s is not None else (det.end_timeout + det.offline_after + 2)
        end = last_t + tail
        while next_tick <= end:
            do_tick(next_tick)
            next_tick += 1.0
    evs = sorted(events.values(), key=lambda e: e["id"])
    return {"events": evs, "ticks": ticks, "detector": det}


def summarize(result: dict, learn_end: float | None = None) -> dict:
    ticks = result["ticks"]
    sc = np.array([s for (t, s, st, _) in ticks if st in ("clear", "motion")
                   and (learn_end is None or t >= learn_end)])
    out = {"ticks": len(ticks), "events": len(result["events"])}
    if len(sc):
        out.update(score_p50=float(np.median(sc)), score_p99=float(np.percentile(sc, 99)),
                   score_max=float(sc.max()), judged_ticks=int(len(sc)))
    return out


def read_truth(path: str) -> dict[str, list[tuple[int, int, int]]]:
    by_src: dict[str, list] = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            by_src.setdefault(row["src"], []).append((int(row["seq"]), int(row["t_us"]), int(row["motion"])))
    return by_src


def truth_bursts(samples, truth_csv: str) -> list[tuple[float, float]]:
    """fake_esp32 --truth CSV -> motion intervals in replay time (matched by (src, seq))."""
    truth = read_truth(truth_csv)
    tmap = {(src, p["seq"]): t for t, src, kind, p in samples if kind == "esp32"}
    flags = []
    for src, lst in truth.items():
        for seq, _tus, m in lst:
            if (src, seq) in tmap:
                flags.append((tmap[(src, seq)], bool(m)))
    flags.sort()
    return truth_intervals(flags)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="JSONL / JSONL.gz: wifisense output, udp_dump --save, engine raw/walktest files")
    ap.add_argument("--config", help="config.toml (default: project root)")
    ap.add_argument("--learning-s", type=float, help="override baseline.learning_s")
    ap.add_argument("--sensitivity", choices=LEVELS, help="level to report events for (default: all three)")
    ap.add_argument("--loop", type=int, default=1, help="replay the files N times back to back")
    ap.add_argument("--inject", help="Mac motion windows in seconds from file start, e.g. 300:320,400:430")
    ap.add_argument("--intensity", type=float, default=2.0, help="injected motion strength A (dB), see sim.py")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--truth", help="fake_esp32 --truth CSV to score ESP32 detections against")
    ap.add_argument("--sim", action="store_true", help="generate fake_esp32 frames in-process")
    ap.add_argument("--sim-duration", type=float, default=900)
    ap.add_argument("--burst-every", type=float, default=90)
    ap.add_argument("--burst-len", type=float, default=8)
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--sim-intensity", type=float, default=1.0)
    ap.add_argument("--layout", default="s3")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    args = ap.parse_args(argv)

    over = {}
    if args.learning_s is not None:
        over = {"baseline": {"learning_s": args.learning_s}}
    cfg = load_config(args.config, over)
    learning_s = float(cfg["baseline"]["learning_s"])

    rows = read_lines(args.files) if args.files else []
    mac = [r for r in rows if "csi" not in r]
    other = [r for r in rows if "csi" in r]
    bursts: list[tuple[float, float]] = []
    if mac and (args.loop > 1 or args.inject):
        mac.sort(key=lambda r: r["t"])
        t0 = mac[0]["t"]
        span = mac[-1]["t"] - t0 + 0.1
        looped = []
        for i in range(args.loop):
            looped += shift_rows(mac, i * span)
        mac = looped
        if args.inject:
            for part in args.inject.split(","):
                a, b = part.split(":")
                bursts.append((t0 + float(a), t0 + float(b)))
            mac = inject_mac_motion(mac, bursts, args.intensity, args.seed)
    samples = classify(mac + other)
    if args.sim:
        flags = []
        sim = []
        t0 = samples[0][0] if samples else 0.0
        for t, msg, moving in esp32_stream(args.sim_duration, args.burst_every, args.burst_len, args.nodes,
                                           args.seed or 1, intensity=args.sim_intensity, layout=args.layout, t0=t0):
            sim.append((t, msg["src"], "esp32", msg))
            flags.append((t, moving))
        bursts += truth_intervals(flags)
        samples = sorted(samples + sim, key=lambda x: x[0])
    if args.truth:
        bursts += truth_bursts(samples, args.truth)
    if not samples:
        ap.error("no input: give files and/or --sim")

    t_start = samples[0][0]
    learn_end = t_start + learning_s + 5
    levels = [args.sensitivity] if args.sensitivity else list(LEVELS)
    report = {}
    for lv in levels:
        res = run_replay(samples, cfg, sensitivity=lv)
        summ = summarize(res, learn_end)
        if bursts:
            summ.update({k: v for k, v in score_events(res["events"], bursts).items() if k != "latencies"})
        report[lv] = summ
        if not args.quiet:
            print(f"== sensitivity {lv} (threshold {cfg['detect']['thresholds'][lv]}) ==")
            for ev in res["events"]:
                print("  event #%d start %+.1fs dur %.1fs peak %.2f sources %s%s" % (
                    ev["id"], ev["start"] - t_start, ev["duration_s"], ev["peak_score"], ",".join(ev["sources"]),
                    (" note: " + ev["note"]) if ev.get("note") else ""))
    print(json.dumps({"samples": len(samples), "span_s": round(samples[-1][0] - t_start, 1),
                      "bursts": [(round(a - t_start, 1), round(b - t_start, 1)) for a, b in bursts],
                      "by_level": report}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
