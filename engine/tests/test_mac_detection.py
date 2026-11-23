"""Mac path: real idle capture must stay quiet; synthetic walking injected into it must fire."""
import pytest

from sentry.config import LEVELS
from sentry.sim import inject_mac_motion, score_events

from conftest import SPAN, cfg_with, events_after, looped, replay

BURSTS = [(60, 20), (150, 10), (300, 30), (420, 8), (500, 15)]  # (offset in pass 2, length) s


@pytest.mark.parametrize("level", LEVELS)
def test_idle_short_learning_no_events(idle_rows, level):
    """Learn on the first 3 min, judge the remaining 7 min out of sample."""
    cfg = cfg_with(baseline={"learning_s": 180})
    res = replay(idle_rows, cfg, level)
    assert res["events"] == []
    t0 = idle_rows[0]["t"]
    judged = [s for (t, s, st, _) in res["ticks"] if st == "clear" and t > t0 + 190]
    assert len(judged) > 380
    assert max(judged) < cfg["detect"]["thresholds"]["high"]


@pytest.mark.parametrize("level", LEVELS)
def test_idle_full_learning_no_events(idle_rows, level):
    """Default 10 min learning on pass 1, then the same 10 min again: zero events."""
    cfg = cfg_with()
    res = replay(looped(idle_rows, 2), cfg, level)
    assert res["events"] == []
    states = {st for (_, _, st, _) in res["ticks"][-500:-20]}
    assert states == {"clear"}


def test_injection_is_identity_without_motion(idle_rows):
    out = inject_mac_motion(idle_rows[:3000], [], 2.0, seed=1)
    assert [(r["rssi_c0"], r["rssi_c1"]) for r in out] == [(r["rssi_c0"], r["rssi_c1"]) for r in idle_rows[:3000]]
    # and outside the bursts it is identity too
    t0 = idle_rows[0]["t"]
    out = inject_mac_motion(idle_rows[:3000], [(t0 + 100, t0 + 120)], 2.0, seed=1)
    same = [a["rssi_c0"] == b["rssi_c0"] for a, b in zip(out, idle_rows) if not (t0 + 98 <= a["t"] < t0 + 122)]
    assert all(same)


def _inject_run(idle_rows, intensity, level, seed):
    two = looped(idle_rows, 2)
    t0 = idle_rows[0]["t"]
    bursts = [(t0 + SPAN + s, t0 + SPAN + s + n) for s, n in BURSTS]
    rows = inject_mac_motion(two, bursts, intensity, seed=seed)
    res = replay(rows, cfg_with(), level)
    return score_events(events_after(res, t0 + SPAN), bursts), res


@pytest.mark.parametrize("level", LEVELS)
def test_strong_walk_detected_everywhere(idle_rows, level):
    """A = 3 dB (walking right through the Mac's link): all 5 bursts, every level, no false alarms."""
    for seed in (1, 2):
        sc, _ = _inject_run(idle_rows, 3.0, level, seed)
        assert sc["detected"] == 5, sc
        assert sc["false_alarms"] == 0
        assert sc["latency_max_s"] <= 8.0


@pytest.mark.parametrize("level,min_hits", [("medium", 5), ("high", 5)])
def test_moderate_walk_detected(idle_rows, level, min_hits):
    """A = 2 dB: medium and high catch every burst."""
    for seed in (1, 2, 3):
        sc, _ = _inject_run(idle_rows, 2.0, level, seed)
        assert sc["detected"] >= min_hits, sc
        assert sc["false_alarms"] == 0


def test_weak_walk_mostly_detected_on_high(idle_rows):
    """A = 1.5 dB: high catches most bursts."""
    hits = sum(_inject_run(idle_rows, 1.5, "high", seed)[0]["detected"] for seed in (1, 2, 3))
    assert hits >= 12  # of 15
