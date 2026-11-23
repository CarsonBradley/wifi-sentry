"""Baseline behaviour: steps, steady new disturbances (absorbed), lingering movement (not absorbed),
gaps, channel changes, restart persistence."""
import json
import random

from sentry.config import LEVELS
from sentry.detector import Detector
from sentry.replay import classify, run_replay
from sentry.sim import inject_mac_motion, score_events

from conftest import SPAN, cfg_with, events_after, looped, replay


def _with(rows, t_from, fn):
    out = []
    for r in rows:
        if r["t"] >= t_from:
            r = dict(r)
            fn(r)
        out.append(r)
    return out


def test_step_change_in_mean_rssi(idle_rows):
    """A permanent level step (furniture moved / door closed / AP tx power) opens no event (the
    step-robust variance removes a lone step) and has no lasting effect on the score."""
    two = looped(idle_rows, 2)
    t0 = idle_rows[0]["t"]
    t_step = t0 + SPAN + 200
    for d0, d1 in ((-4, -4), (3, 0), (-6, -2)):
        rows = _with(two, t_step, lambda r: r.update(rssi_c0=r["rssi_c0"] + d0, rssi_c1=r["rssi_c1"] + d1))
        for level in LEVELS:
            res = replay(rows, cfg_with(), level)
            evs = events_after(res, t0 + SPAN)
            assert evs == []
            after = [s for (t, s, st, _) in res["ticks"] if t_step + 4 <= t < t0 + 2 * SPAN - 5]
            assert max(after) < 2.0


def _fan(r, rng, sd=1.0):
    r["rssi_c0"] = int(round(r["rssi_c0"] + rng.uniform(-0.45, 0.45) + rng.gauss(0, sd)))
    r["rssi_c1"] = int(round(r["rssi_c1"] + rng.uniform(-0.45, 0.45) + rng.gauss(0, sd)))


def test_steady_disturbance_is_absorbed_within_bound(idle_rows):
    """A fan switched on raises the variance permanently: one event, absorbed after
    absorb_after_s (+ window), then quiet again."""
    cfg = cfg_with(baseline={"absorb_after_s": 300, "absorb_window_s": 120})
    rows = looped(idle_rows, 4)
    t0 = idle_rows[0]["t"]
    t_on = t0 + SPAN + 100
    rng = random.Random(5)
    rows = _with(rows, t_on, lambda r: _fan(r, rng))
    res = replay(rows, cfg, "medium")
    evs = events_after(res, t0 + SPAN)
    assert len(evs) == 1, evs
    ev = evs[0]
    assert "absorbed" in ev["note"]
    assert abs(ev["start"] - t_on) < 5
    assert ev["end"] - t_on <= 300 + 120 + 30  # bounded
    late = [s for (t, s, st, _) in res["ticks"] if ev["end"] + 15 < t < t0 + 4 * SPAN - 5]
    assert max(late) < cfg["detect"]["thresholds"]["medium"]
    bl = res["detector"].sources["mac"].baselines["rssi"]
    assert bl.absorbed == 1 and bl.median > 0.8  # baseline now reflects the fan


def test_lingering_person_not_absorbed(idle_rows):
    """Intermittent movement for 20 minutes is never learned as normal, and is still detected."""
    cfg = cfg_with(baseline={"absorb_after_s": 300, "absorb_window_s": 120})
    rows = looped(idle_rows, 4)
    t0 = idle_rows[0]["t"]
    start = t0 + SPAN + 60
    bursts, t = [], start
    rng = random.Random(3)
    while t < start + 1200:
        n = rng.uniform(8, 20)
        bursts.append((t, t + n))
        t += n + rng.uniform(25, 50)
    final = (t0 + 4 * SPAN - 200, t0 + 4 * SPAN - 180)
    rows = inject_mac_motion(rows, bursts + [final], 2.0, seed=7)
    res = replay(rows, cfg, "medium")
    det = res["detector"]
    bl = det.sources["mac"].baselines["rssi"]
    assert bl.absorbed == 0
    assert bl.median < 0.35  # idle median is ~0.21 dB^2
    sc = score_events(events_after(res, t0 + SPAN), [final])
    assert sc["detected"] == 1
    assert all("absorbed" not in e["note"] for e in res["events"])


def test_gap_and_channel_change(idle_rows):
    two = looped(idle_rows, 2)
    t0 = idle_rows[0]["t"]
    gap = (t0 + SPAN + 100, t0 + SPAN + 130)
    rows = [r for r in two if not (gap[0] <= r["t"] < gap[1])]
    rows = _with(rows, t0 + SPAN + 300, lambda r: r.update(channel=149))
    for level in LEVELS:
        res = replay(rows, cfg_with(), level)
        assert events_after(res, t0 + SPAN) == []
        states = [st for (t, s, st, _) in res["ticks"] if gap[0] + 6 < t < gap[1]]
        assert set(states) == {"offline"}
        after = [st for (t, s, st, _) in res["ticks"] if gap[1] + 5 < t < gap[1] + 60]
        assert set(after) == {"clear"}  # baseline survives a gap, no relearning
    assert res["detector"].sources["mac"].scorer.n_channel_changes == 1


def test_restart_persistence(idle_rows):
    """Baselines saved after pass 1 are reused: pass 2 starts 'clear' immediately, no events,
    and injected motion is still detected without a new learning period."""
    cfg = cfg_with()
    d1 = Detector(cfg)
    run_replay(classify(idle_rows), cfg, detector=d1)
    assert d1.sources["mac"].baselines["rssi"].learned
    saved = json.loads(json.dumps(d1.export_baselines()))

    d2 = Detector(cfg)
    d2.import_baselines(saved)
    t0 = idle_rows[0]["t"]
    rows = [dict(r, t=r["t"] + 3600) for r in idle_rows]
    bursts = [(t0 + 3600 + 120, t0 + 3600 + 140)]
    rows = inject_mac_motion(rows, bursts, 2.5, seed=2)
    res = run_replay(classify(rows), cfg, detector=d2)
    states = [st for (t, s, st, _) in res["ticks"]]
    assert "learning" not in states[6:]
    assert states[6] == "clear"
    sc = score_events(res["events"], bursts)
    assert sc["detected"] == 1 and sc["false_alarms"] == 0


def test_relearn_resets(idle_rows):
    cfg = cfg_with(baseline={"learning_s": 120})
    det = Detector(cfg)
    run_replay(classify(idle_rows[:2000]), cfg, detector=det, tail_s=0)
    assert det.state == "clear"
    det.relearn(det.last_t)
    res = run_replay(classify(idle_rows[2000:2300]), cfg, detector=det, tail_s=0)
    assert res["ticks"][-1][2] == "learning"


def test_blind_source_reports_offline(idle_rows):
    """Per-chain RSSI vanishing (private API gone) or freezing on a silent link (no packets arriving)
    must not read as 'clear'. Unchanged readings WITH traffic are a still room (see
    test_night_artifacts) and only count as stuck after mac_stuck_s."""
    two = looped(idle_rows, 2)
    t0 = idle_rows[0]["t"]
    t_off = t0 + SPAN + 100
    rows = _with(two, t_off, lambda r: r.update(rssi_c0=None, rssi_c1=None))
    res = replay(rows, cfg_with(), "medium")
    late = {st for (t, s, st, _) in res["ticks"] if t_off + 20 < t < t0 + 2 * SPAN - 5}
    assert late == {"offline"}
    assert res["detector"].sources["mac"].scorer.health(t_off + 30).startswith("per-chain RSSI unavailable")
    frozen = _with(two, t_off, lambda r: r.update(rssi_c0=-42, rssi_c1=-43, rx_pkts=123456))
    res = replay(frozen, cfg_with(), "medium")
    late = {st for (t, s, st, _) in res["ticks"] if t_off + 30 < t < t0 + 2 * SPAN - 5}
    assert late == {"offline"}
    assert events_after(res, t0 + SPAN) == []
    still = _with(two, t_off, lambda r: r.update(rssi_c0=-42, rssi_c1=-43))  # packets keep arriving
    res = replay(still, cfg_with(sources={"mac_stuck_s": 120.0}), "medium")
    states = {t: st for (t, s, st, _) in res["ticks"]}
    assert {st for t, st in states.items() if t_off + 5 < t < t_off + 115} == {"clear"}
    assert {st for t, st in states.items() if t_off + 135 < t < t0 + 2 * SPAN - 5} == {"offline"}
    assert events_after(res, t0 + SPAN) == []
