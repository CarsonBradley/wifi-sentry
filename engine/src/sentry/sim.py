"""Synthetic inputs for replay and tests.

Mac motion injection model (applied to REAL idle wifisense rows)
-----------------------------------------------------------------
The recorded per-chain RSSI is an integer reading x of a continuous value.
We re-quantise x + u + m(t), with u ~ U(-0.45, 0.45) per sample, so the row is
unchanged when m = 0 and the idle dither/noise of the real capture is kept.
During a "walk" the perturbation for chain i is

    m_i(t) = s(t) + f_i(t)

  * s(t): body shadowing, common to both chains. Ornstein-Uhlenbeck process
    (time constant 1.5 s) scaled so its sd is 0.6 * A dB and biased
    to -0.5 * A dB (a body mostly attenuates), ramped in/out over 1 s.
  * f_i(t): multipath from the moving body, independent per chain (the
    antennas are more than half a wavelength apart at 5.8 GHz):
    20*log10|1 + a * exp(j*phi_i(t))| with a = 0.12 * A, where phi_i
    advances at 2*pi*v(t)/lambda, lambda = 5.2 cm, |v| = path-length rate
    0.3-1.2 m/s (OU-varying walking speed) - i.e. far faster than the 10 Hz
    sampling, so it aliases to noise-like fluctuation - then smoothed by a
    first-order filter (tau 0.3 s) because the driver's per-chain value
    appears to average several frames.

A = "intensity" in dB. A = 1 is a person a few metres off the link (+/-1 dB),
A = 2 a person walking near/through the link (+/-2-3 dB), A = 4 crossing right
next to the Mac. These magnitudes are assumptions until the walk test measures
a real room; published RSSI device-free-detection work reports 1-5 dB
standard deviation for a walker near a link versus ~0.5 dB idle.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from .config import PROJECT_ROOT

LAMBDA_5G = 0.052  # m, channel 157


def inject_mac_motion(rows: list[dict], bursts: list[tuple[float, float]], intensity: float = 2.0,
                      seed: int = 0) -> list[dict]:
    """Return copies of rows with walking motion injected in [start, end) windows (data time)."""
    rng = random.Random(seed)
    out = []
    a = min(0.9, 0.12 * intensity)
    sh = 0.0
    phi = [rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)]
    fsm = [0.0, 0.0]
    v = 0.8
    last_t = None
    for row in rows:
        t = row["t"]
        dt = 0.1 if last_t is None else max(0.0, min(1.0, t - last_t))
        last_t = t
        ramp = 0.0
        for s, e in bursts:
            if s - 1.0 <= t < e + 1.0:
                ramp = max(ramp, min(1.0, (t - (s - 1.0)), ((e + 1.0) - t)))
        r = dict(row)
        if ramp > 0 and row.get("rssi_c0") is not None and row.get("rssi_c1") is not None:
            # body shadowing (common)
            tau = 1.5
            sh += -sh * dt / tau + 0.6 * intensity * math.sqrt(2 * dt / tau) * rng.gauss(0, 1)
            v += -0.8 * (v - 0.8) * dt + 0.6 * math.sqrt(dt) * rng.gauss(0, 1)
            v = max(0.3, min(1.2, abs(v)))
            m = []
            for i in range(2):
                phi[i] += 2 * math.pi * v * dt / LAMBDA_5G * (1 if i == 0 else 0.93) + rng.gauss(0, 0.5)
                f = 20 * math.log10(max(1e-3, abs(1 + a * complex(math.cos(phi[i]), math.sin(phi[i])))))
                fsm[i] += (f - fsm[i]) * min(1.0, dt / 0.3)
                m.append(ramp * (sh - 0.5 * intensity + fsm[i]))
            for i, k in enumerate(("rssi_c0", "rssi_c1")):
                u = rng.uniform(-0.45, 0.45)
                r[k] = int(round(row[k] + u + m[i]))
        else:
            sh *= 0.9
        out.append(r)
    return out


def shift_rows(rows: list[dict], dt: float) -> list[dict]:
    return [dict(r, t=r["t"] + dt) for r in rows]


def load_fake_esp32():
    p = str(PROJECT_ROOT / "esp32" / "tools")
    if p not in sys.path:
        sys.path.insert(0, p)
    import fake_esp32  # noqa: E402

    return fake_esp32


def esp32_stream(duration: float, burst_every: float = 0.0, burst_len: float = 5.0, nodes: int = 1,
                 seed: int = 1, rate: float = 50.0, intensity: float = 1.0, loss: float = 0.005,
                 layout: str = "s3", name: str = "fake", t0: float = 0.0, motion: bool = False):
    """In-process fake_esp32: yields (t, msg_dict, moving) in time order, mirroring its main loop
    (same RNG streams, so the same seed gives the same frames as the CLI tool)."""
    fe = load_fake_esp32()

    class A:  # the argparse namespace motion_at() expects
        pass

    args = A()
    args.motion, args.burst_every, args.burst_len = motion, burst_every, burst_len
    master = random.Random(seed)
    ns = []
    for i in range(nodes):
        nm = name if nodes == 1 else "%s%d" % (name, i + 1)
        ns.append(fe.Node(nm, fe.LAYOUTS[layout], random.Random(master.getrandbits(64)), 6))
    timing = random.Random(master.getrandbits(64))
    lossr = random.Random(master.getrandbits(64))
    period = 1.0 / rate
    next_t = [timing.uniform(0, period) for _ in ns]
    while True:
        i = min(range(len(ns)), key=lambda j: next_t[j])
        t = next_t[i]
        if t >= duration:
            return
        node = ns[i]
        dt = max(0.001, period + timing.gauss(0, 1.5 / 1000.0))
        next_t[i] = t + dt
        moving = fe.motion_at(t, args)
        msg = node.step(dt if node.seq else 0.0, moving, intensity)
        if lossr.random() >= loss:
            yield t0 + t, msg, moving


def truth_intervals(flags: list[tuple[float, bool]], merge_gap: float = 0.5) -> list[tuple[float, float]]:
    """Collapse time-ordered (t, moving) flags into [start, end] motion intervals; intervals closer
    than merge_gap (frames of several boards interleaving at a boundary) are merged."""
    out, start, last = [], None, None
    for t, m in flags:
        if m and start is None:
            start = t
        if not m and start is not None:
            out.append((start, last))
            start = None
        last = t
    if start is not None:
        out.append((start, last))
    merged: list[tuple[float, float]] = []
    for a, b in out:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def score_events(events: list[dict], bursts: list[tuple[float, float]], slack: float = 15.0) -> dict:
    """Match events to ground-truth bursts. events need start/end/opened_at."""
    hit, lat = 0, []
    used = set()
    for bs, be in bursts:
        best = None
        for ev in events:
            es = ev["start"]
            ee = ev["end"] if ev["end"] is not None else math.inf
            if es <= be + slack and ee >= bs:
                best = ev
                break
        if best is not None:
            hit += 1
            used.add(best["id"])
            lat.append(max(0.0, best.get("opened_at", best["start"]) - bs))
    false = [ev for ev in events if ev["id"] not in used and not any(
        ev["start"] <= be + slack and (ev["end"] or math.inf) >= bs - slack for bs, be in bursts)]
    return {
        "bursts": len(bursts),
        "detected": hit,
        "detection_rate": hit / len(bursts) if bursts else None,
        "false_alarms": len(false),
        "latency_mean_s": sum(lat) / len(lat) if lat else None,
        "latency_max_s": max(lat) if lat else None,
        "latencies": lat,
    }
