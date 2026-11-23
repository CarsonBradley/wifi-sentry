"""Macs that expose fewer (or more) signals than the one wifi-sentry was built on.

All of this is SIMULATED: the per-antenna API was only ever observed on one Mac (2 chains, macOS 27.0.1).
These tests feed synthetic sampler output (the real idle capture with antennas removed or added, and
the {"type":"caps"} lines wifisense prints) through the live detection code."""
import asyncio
import time
import json
import random
import sys

import numpy as np
import pytest

from sentry.detector import Detector
from sentry.fake_sampler import caps_for, transform
from sentry.features import MacScorer, common_pulse_mask, pulse_mask
from sentry.replay import classify, run_replay
from sentry.sim import inject_mac_motion
from sentry.sources import MacSource

from conftest import cfg_with


def with_caps(rows, mode, seed=1):
    """Rows as a sampler in `mode` would print them, preceded by its caps line."""
    rng = random.Random(seed)
    out = [caps_for(mode, rows[0]["t"] - 0.05)]
    out += [dict(transform(r, mode, rng), t=r["t"]) for r in rows]
    return out


def run(rows, cfg=None, level="medium", tail_s=0.0):
    cfg = cfg or cfg_with(baseline={"learning_s": 120})
    return run_replay(classify(rows), cfg, sensitivity=level, tail_s=tail_s)


def last_status(res):
    det = res["detector"]
    return det.status(det.last_t)


# ---------------------------------------------------------------- the original 2-antenna case
def test_dual_with_caps_is_identical_to_no_caps(idle_rows):
    rows = idle_rows[:3000]
    a = run(rows, tail_s=None)
    b = run(with_caps(rows, "dual"), tail_s=None)
    assert [t[:3] for t in a["ticks"]] == [t[:3] for t in b["ticks"]]
    a = run(rows)
    b = run(with_caps(rows, "dual"))
    assert [t[:3] for t in a["ticks"]] == [t[:3] for t in b["ticks"]]
    st = last_status(b)
    src = st["sources"][0]
    assert src["signal_mode"] == "dual" and src["notice"] is None and st["sensing"] == "full"
    assert b["detector"].sources["mac"].key == "rssi"  # saved 0.1.x baselines keep matching


def test_caps_line_is_not_a_sample(idle_rows):
    d = Detector(cfg_with())
    st = d.ingest("mac", "mac", caps_for("dual", 100.0), 100.0)
    assert st.last_seen is None and len(st.arrivals) == 0
    assert st.scorer.caps["chains"] == 2


# ---------------------------------------------------------------- one antenna
def test_single_antenna_scores_and_says_reduced(idle_rows):
    rows = idle_rows[:3000]
    t0 = rows[0]["t"]
    walk = inject_mac_motion(rows, [(t0 + 200, t0 + 230)], 3.0, seed=4)
    res = run(with_caps(walk, "single"))
    st = last_status(res)
    src = st["sources"][0]
    assert src["signal_mode"] == "single" and src["health"] is None
    assert src["notice_level"] == "reduced" and "one antenna" in src["notice"]
    assert st["sensing"] == "reduced" and st["state"] in ("clear", "motion")
    assert src["rssi_c1"] is None and src["rssi_c0"] is not None
    assert res["detector"].sources["mac"].key == "rssi1"
    assert any(t0 + 195 <= e["start"] <= t0 + 235 for e in res["events"]), res["events"]


def test_single_antenna_idle_is_quiet_on_low(idle_rows):
    res = run(with_caps(idle_rows[:6000], "single"), level="low")
    assert len(res["events"]) <= 1


# ---------------------------------------------------------------- three antennas
def test_three_antennas_use_all_chains(idle_rows):
    rows = idle_rows[:3000]
    t0 = rows[0]["t"]
    walk = inject_mac_motion(rows, [(t0 + 200, t0 + 230)], 3.0, seed=5)
    res = run(with_caps(walk, "multi"))
    src = last_status(res)["sources"][0]
    assert src["signal_mode"] == "multi" and len(src["rssi_chains"]) == 3 and src["notice"] is None
    assert res["detector"].sources["mac"].key == "rssi3"
    assert any(t0 + 195 <= e["start"] <= t0 + 235 for e in res["events"])


def test_multi_feature_reduces_to_dual_formula():
    """For two chains the N-chain feature is exactly V(d) + w V(c)."""
    cfg = cfg_with(detect={"mac_pulse_reject": False, "mac_step_robust": False})
    sc = MacScorer(cfg)
    rng = np.random.default_rng(0)
    c0 = rng.integers(-45, -40, 40)
    c1 = rng.integers(-46, -41, 40)
    for i in range(40):
        sc.add({"rssi_c0": int(c0[i]), "rssi_c1": int(c1[i])}, 100 + i * 0.1)
    t = 100 + 3.9
    win = slice(10, 40)
    want = np.var((c0[win] - c1[win]) / 2) + np.var((c0[win] + c1[win]) / 2)
    assert abs(sc.feature(t) - want) < 1e-12
    x = np.stack([c0[win], c1[win]], 1).astype(float)
    c = x.mean(1)
    assert abs(np.mean([np.var(x[:, j] - c) for j in range(2)]) + np.var(c) - want) < 1e-12


# ---------------------------------------------------------------- no per-antenna API
def test_aggregate_fallback_is_loudly_degraded(idle_rows):
    res = run(with_caps(idle_rows[:3000], "aggregate"))
    st = last_status(res)
    src = st["sources"][0]
    assert src["signal_mode"] == "aggregate" and src["health"] is None
    assert src["notice_level"] == "degraded"
    assert "doesn't expose per-antenna signal, detection will be poor" in src["notice"]
    assert st["sensing"] == "degraded"
    assert st["state"] in ("clear", "motion")  # it does score: never silently blind...
    assert res["detector"].sources["mac"].key == "rssi_agg"


def test_aggregate_unchanged_for_long_is_not_stuck(idle_rows):
    rows = [dict(r, rssi=-40) for r in idle_rows[:8000]]  # the smoothed value can sit still for minutes
    res = run(with_caps(rows, "aggregate"), cfg_with(sources={"mac_stuck_s": 120.0}, baseline={"learning_s": 60}))
    assert last_status(res)["sources"][0]["health"] is None


def test_status_pushes_when_mode_changes(idle_rows):
    """API vanishes mid-run (macOS update while running): the sampler re-announces caps, the engine
    switches to the aggregate fallback and pushes the degraded notice."""
    rows = idle_rows[:2400]
    half = rows[1200]["t"]
    stream = [caps_for("dual", rows[0]["t"] - 0.1)] + rows[:1200]
    stream += [caps_for("aggregate", half - 0.05)] + [dict(transform(r, "aggregate", random.Random(1)), t=r["t"])
                                                       for r in rows[1200:]]
    d = Detector(cfg_with(baseline={"learning_s": 60}))
    pushed = []
    run_replay(classify(stream), d.cfg, detector=d,
               on_tick=lambda det, res: pushed.extend(det.status(det.last_t)["sources"][0]["notice_level"]
                                                      for k, _ in res.emits if k == "status"))
    assert "degraded" in pushed
    assert d.sources["mac"].scorer.mode == "aggregate"
    assert set(d.sources["mac"].baselines) == {"rssi", "rssi_agg"}  # never mixed into the 2-chain baseline


# ---------------------------------------------------------------- not on Wi-Fi
@pytest.mark.parametrize("link,words", [("off", "turned off"), ("disconnected", "not connected"),
                                        ("none", "no Wi-Fi interface")])
def test_no_wifi_link_is_a_clear_health_message(idle_rows, link, words):
    res = run(with_caps(idle_rows[:600], link))
    st = last_status(res)
    src = st["sources"][0]
    assert words in src["health"]
    assert st["state"] == "offline" and st["sensing"] == "none"
    assert res["events"] == []


def test_link_back_up_resumes(idle_rows):
    rows = idle_rows[:3000]
    stream = [caps_for("off", rows[0]["t"] - 0.1)] + [dict(transform(r, "off", random.Random(1)), t=r["t"]) for r in rows[:300]]
    stream += [caps_for("dual", rows[300]["t"] - 0.05)] + rows[300:]
    res = run(stream)
    assert last_status(res)["state"] in ("clear", "motion")


# ---------------------------------------------------------------- generalised pulse mask
def _box(n=80, at=40, width=6, up=(3, 3, 3)):
    ts = np.arange(n) * 0.1
    chains = []
    for k, u in enumerate(up):
        x = np.full(n, -42.0 - k)
        x[at:at + width] += u
        chains.append(x)
    return ts, chains


def test_pulse_mask_two_chains_matches_original():
    ts, ch = _box(up=(3, 3))
    assert (pulse_mask(ts, ch) == common_pulse_mask(ts, ch[0], ch[1])).all()
    assert pulse_mask(ts, ch)[40:46].all()


def test_pulse_mask_three_chains():
    ts, ch = _box(up=(3, 3, 3))
    assert pulse_mask(ts, ch)[40:46].all()          # common box on every antenna: artifact
    ts, ch = _box(up=(3, 3, 0))
    assert not pulse_mask(ts, ch).any()             # one antenna disagrees: possibly a body, kept


def test_pulse_mask_single_chain_masks_any_short_box():
    ts, ch = _box(up=(3,))
    assert pulse_mask(ts, ch)[40:46].all()
    ts, ch = _box(up=(-3,))
    assert not pulse_mask(ts, ch).any()             # dips are never masked


# ---------------------------------------------------------------- the stand-in sampler through MacSource
def test_fake_sampler_through_macsource():
    async def go():
        got = []
        src = MacSource([sys.executable, "-m", "sentry.fake_sampler", "--mode", "single", "--hz", "50"], ".",
                        lambda s, k, p, t, raw: got.append(p))
        src.start()
        deadline = time.monotonic() + 30   # a cold Python + numpy start on a CI runner can take seconds
        while len(got) <= 5 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await src.stop()
        return got
    got = asyncio.run(go())
    assert got[0]["type"] == "caps" and got[0]["chains"] == 1
    assert "rssi_c1" not in got[1] and isinstance(got[1]["rssi_c0"], int)
    json.dumps(got)
