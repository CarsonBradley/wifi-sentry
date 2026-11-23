"""Regression tests from the first full night (2026-11-23, nobody moving, quiet link).

Fixtures (lines copied unchanged from the engine's raw recordings of that night, hours 03 and 04):
  night-20261123-0300-0326: busier dither regime; 5 common-mode "boxes" (old events #12-#16,
                            including the 7 s event #14 at 03:09:57).
  night-20261123-0327-0405: quiet regime (c0 == c1 most of the time, readings unchanged for up to
                            105 s while ~80 rx packets/s arrive); 13 boxes (old events #18-#27) and
                            the cluster of 'source_offline' gaps the frozen-reading rule produced.
The excluded 70 s between them hold old event #17 (03:27:24), a brief differential wobble that is
not the box artifact and is deliberately still scored.
"""
import gzip
import json
from pathlib import Path

import numpy as np
import pytest

from sentry.features import MacScorer, common_pulse_mask
from sentry.sim import inject_mac_motion, score_events

from conftest import cfg_with, replay

FIX = Path(__file__).parent / "fixtures"
SLICES = ("night-20261123-0300-0326", "night-20261123-0327-0405")
OLD = {"detect": {"mac_pulse_reject": False}, "sources": {"mac_stuck_s": 15.0}}  # the pre-fix detector


def _load(name: str) -> list[dict]:
    with gzip.open(FIX / f"{name}.jsonl.gz", "rt") as f:
        rows = [json.loads(line) for line in f]
    rows.sort(key=lambda r: r["t"])
    return rows


@pytest.fixture(scope="module")
def night() -> dict[str, list[dict]]:
    return {n: _load(n) for n in SLICES}


@pytest.mark.parametrize("name", SLICES)
@pytest.mark.parametrize("level", ["low", "medium"])
def test_no_events_on_night_artifacts(night, name, level):
    res = replay(night[name], cfg_with(baseline={"learning_s": 180}), level)
    assert res["events"] == [], [(e["start"], e["peak_score"]) for e in res["events"]]


@pytest.mark.parametrize("name,min_old", [(SLICES[0], 3), (SLICES[1], 8)])
def test_fixture_really_contains_the_artifact(night, name, min_old):
    """Guard against the fixture silently going stale: the pre-fix detector fires on it."""
    old = cfg_with(baseline={"learning_s": 180}, **OLD)
    assert len(replay(night[name], old, "medium")["events"]) >= min_old


def test_high_level_stays_quiet_on_night_artifacts(night):
    for name in SLICES:
        res = replay(night[name], cfg_with(baseline={"learning_s": 180}), "high")
        assert res["events"] == []


def test_every_box_in_the_fixture_is_masked(night):
    """Each +3/+4 dB common-mode box (both chains >= +2 dB above the preceding 5 s) is masked."""
    rows = night[SLICES[1]]
    ts = np.array([r["t"] for r in rows])
    c0 = np.array([r["rssi_c0"] for r in rows], float)
    c1 = np.array([r["rssi_c1"] for r in rows], float)
    mask = common_pulse_mask(ts, c0, c1)
    boxes = 0
    k = 60
    while k < len(ts):
        r0, r1 = np.median(c0[k - 60:k - 10]), np.median(c1[k - 60:k - 10])
        if c0[k] - r0 >= 3 and c1[k] - r1 >= 3:
            boxes += 1
            span = slice(k, k + 10)
            big = (c0[span] - r0 >= 2) & (c1[span] - r1 >= 2)
            assert mask[span][big].all(), ts[k]
            k += 30
        else:
            k += 1
    assert boxes >= 10
    assert mask.mean() < 0.01  # it removes the boxes, not the signal


def test_quiet_link_is_not_reported_offline(night):
    """Readings that sit still while packets keep arriving are a still room: keep watching."""
    rows = night[SLICES[1]]
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    new = replay(rows, cfg_with(baseline={"learning_s": 180}), "medium")
    assert {st for (t, _, st, _) in new["ticks"] if t0 + 200 < t < t1} == {"clear"}
    old = replay(rows, cfg_with(baseline={"learning_s": 180}, **OLD), "medium")
    assert sum(st == "offline" for (_, _, st, _) in old["ticks"]) > 300  # what the live engine showed


def test_motion_still_detected_on_the_quiet_link(night):
    """Injected walking (A = 3 dB, 8-30 s) on the quiet-link slice opens an event for every burst."""
    rows = night[SLICES[1]]
    t0 = rows[0]["t"]
    bursts = [(t0 + s, t0 + s + n) for s, n in ((600, 8), (900, 15), (1200, 30), (1600, 10), (1900, 20))]
    for seed in (1, 2):
        res = replay(inject_mac_motion(rows, bursts, 3.0, seed=seed), cfg_with(baseline={"learning_s": 180}),
                     "medium")
        sc = score_events(res["events"], bursts)
        assert sc["detected"] == 5 and sc["false_alarms"] == 0, sc


# ---------------------------------------------------------------- common_pulse_mask unit cases
def _series(shape, n_pre=40, base=(-42, -42)):
    ts = np.arange(n_pre + len(shape) + 20) * 0.1
    c0 = np.full(len(ts), float(base[0]))
    c1 = np.full(len(ts), float(base[1]))
    for i, (a, b) in enumerate(shape):
        c0[n_pre + i] += a
        c1[n_pre + i] += b
    return ts, c0, c1, n_pre


def test_mask_removes_a_common_box_with_ramps():
    shape = [(1, 1), (2, 2), (4, 4), (4, 4), (4, 4), (4, 4), (4, 4), (3, 3), (2, 1), (1, 1)]
    ts, c0, c1, p = _series(shape)
    m = common_pulse_mask(ts, c0, c1)
    assert m[p + 1:p + 9].all() and not m[:p - 1].any() and not m[p + 12:].any()


@pytest.mark.parametrize("shape,why", [
    ([(-4, -4)] * 8, "negative dip (body shadowing) is kept"),
    ([(4, 0)] * 8, "one antenna only is kept"),
    ([(4, 1)] * 8, "differential change > 1 dB is kept"),
    ([(4, 4)] * 25, "longer than 2 s is kept"),
    ([(1, 1)] * 8, "below 2 dB is kept"),
])
def test_mask_leaves_motion_like_shapes(shape, why):
    ts, c0, c1, p = _series(shape)
    assert not common_pulse_mask(ts, c0, c1).any(), why


def test_mask_needs_an_idle_lead_in():
    ts, c0, c1, p = _series([(4, 4)] * 8)
    c0[p - 20:p - 10] += 3  # someone already moving before the box: do not touch it
    c1[p - 15:p - 5] -= 2
    assert not common_pulse_mask(ts, c0, c1)[p:p + 8].any()


def test_pending_box_is_masked_until_it_outlasts_max_s():
    ts, c0, c1, p = _series([(4, 4)] * 25)
    assert common_pulse_mask(ts[:p + 10], c0[:p + 10], c1[:p + 10])[p:p + 10].all()  # 1 s in: pending
    assert not common_pulse_mask(ts[:p + 25], c0[:p + 25], c1[:p + 25]).any()  # 2.5 s in: real rise


# ---------------------------------------------------------------- frozen vs stuck health
def _feed(sc, n, t0, rx_step):
    for i in range(n):
        sc.add({"rssi_c0": -42, "rssi_c1": -42, "rx_pkts": 1000 + i * rx_step}, t0 + i * 0.1)


def test_frozen_needs_a_silent_link():
    cfg = cfg_with(sources={"mac_stuck_s": 120.0})
    busy = MacScorer(cfg)
    _feed(busy, 600, 0.0, 8)  # 60 s unchanged, 80 packets/s
    assert busy.health(60.0) is None
    _feed(busy, 700, 60.0, 8)
    assert "stuck" in busy.health(130.0)
    silent = MacScorer(cfg)
    _feed(silent, 300, 0.0, 0)  # unchanged and no packets
    assert silent.health(30.0).startswith("per-chain RSSI frozen")
    legacy = MacScorer(cfg)  # no packet counter in the stream: old 15 s rule
    for i in range(300):
        legacy.add({"rssi_c0": -42, "rssi_c1": -42}, i * 0.1)
    assert legacy.health(30.0).startswith("per-chain RSSI frozen")
