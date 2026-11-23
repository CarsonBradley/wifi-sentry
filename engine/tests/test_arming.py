"""Arming: exit delay, weekly schedule (local time, DST), manual override until the next boundary, restarts."""
import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sentry.app import create_app
from sentry.arming import Arming, Schedule, local_zone, normalize_windows

from test_api import feed, make_engine, scenario

NY = ZoneInfo("America/New_York")   # DST 2026: starts Sun 8 Mar 02:00, ends Sun 1 Nov 02:00
WEEKDAYS = [0, 1, 2, 3, 4]


class KV:
    """Stand-in for storage.Storage's key/value part (persists across Arming instances = restarts)."""

    def __init__(self):
        self.d, self.u = {}, {}

    def kv_get(self, k, default=None):
        return self.d.get(k, default)

    def kv_set(self, k, v):
        self.d[k], self.u[k] = v, time.time()

    def kv_updated(self, k):
        return self.u.get(k)


def ts(y, mo, d, h=0, mi=0, s=0, tz=NY, fold=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=tz, fold=fold).timestamp()


def with_schedule(kv, windows, now, tz=NY, delay=60):
    a = Arming(kv, delay, tz=tz)
    a.set_settings(now, schedule={"enabled": True, "windows": windows})
    a.manual = None   # let the schedule govern from the start (saving normally holds the current state)
    kv.kv_set("arm_manual", None)
    return a


# ---------------------------------------------------------------- exit delay
def test_exit_delay_counts_down_and_disarm_cancels():
    kv = KV()
    a = Arming(kv, 60, tz=NY)
    t = 1_000_000.0
    assert a.evaluate(t) is False and a.mode(t) == "manual"
    a.set_manual(True, t, 60)
    p = a.public(t + 30)
    assert p["armed"] is False and p["mode"] == "delay" and p["target"] is True and p["arm_at"] == t + 60
    assert p["next_change"] == {"t": t + 60, "armed": True}
    a.set_manual(True, t + 40, 60)            # tapping arm again doesn't restart the countdown
    assert a.public(t + 50)["arm_at"] == t + 60
    assert a.evaluate(t + 59.9) is False and a.evaluate(t + 60) is True and a.mode(t + 61) == "manual"
    a.set_manual(True, t + 100, 60)           # already armed: no new delay
    assert a.evaluate(t + 100) is True
    a.set_manual(False, t + 200)
    a.set_manual(True, t + 300, 60)
    a.set_manual(False, t + 310)              # disarm during the delay cancels it
    assert a.evaluate(t + 400) is False and a.public(t + 400)["next_change"] is None
    a.set_manual(True, t + 500, 0)            # no delay: at once
    assert a.evaluate(t + 500) is True
    with pytest.raises(ValueError):
        a.set_manual(True, t, 7200)


def test_exit_delay_survives_a_restart():
    kv = KV()
    t = 2_000_000.0
    Arming(kv, 60, tz=NY).set_manual(True, t, 60)
    b = Arming(kv, 60, tz=NY)                 # restart during the delay
    assert b.mode(t + 20) == "delay" and b.evaluate(t + 20) is False
    c = Arming(kv, 60, tz=NY)                 # restart after it ran out while the engine was down
    assert c.evaluate(t + 3600) is True and c.mode(t + 3600) == "manual"


def test_legacy_armed_flag_is_migrated():
    kv = KV()
    kv.kv_set("armed", True)
    a = Arming(kv, 60, tz=NY)
    assert a.evaluate(time.time()) is True and kv.kv_get("arm_manual")["armed"] is True


# ---------------------------------------------------------------- schedule
def test_weekday_office_hours():
    kv = KV()
    a = with_schedule(kv, [{"days": WEEKDAYS, "start": "09:00", "end": "17:30"}], ts(2026, 11, 23, 8))
    # Monday 23 Nov 2026
    assert a.evaluate(ts(2026, 11, 23, 8, 59, 59)) is False
    assert a.evaluate(ts(2026, 11, 23, 9)) is True and a.mode(ts(2026, 11, 23, 9)) == "schedule"
    assert a.evaluate(ts(2026, 11, 23, 17, 29, 59)) is True
    assert a.evaluate(ts(2026, 11, 23, 17, 30)) is False
    assert a.evaluate(ts(2026, 11, 28, 12)) is False   # Saturday
    p = a.public(ts(2026, 11, 27, 18))                 # Friday evening: next arm is Monday 09:00
    assert p["next_change"] == {"t": ts(2026, 11, 30, 9), "armed": True} and p["scheduled"] is False


def test_overnight_window_runs_past_midnight():
    kv = KV()
    a = with_schedule(kv, [{"days": [4], "start": "22:00", "end": "06:00"}], ts(2026, 11, 27, 12))  # Fri night
    assert a.evaluate(ts(2026, 11, 27, 21, 59)) is False
    assert a.evaluate(ts(2026, 11, 27, 23, 30)) is True
    assert a.evaluate(ts(2026, 11, 28, 5, 59)) is True    # Saturday morning, still Friday's window
    assert a.evaluate(ts(2026, 11, 28, 6, 0)) is False
    assert a.evaluate(ts(2026, 11, 28, 23, 0)) is False   # Saturday night: not scheduled


def test_dst_fall_back_keeps_wall_clock_times():
    kv = KV()
    # Every night 22:00-06:00; the night of Sat 31 Oct -> Sun 1 Nov 2026 has 25 hours.
    a = with_schedule(kv, [{"days": list(range(7)), "start": "22:00", "end": "06:00"}], ts(2026, 10, 31, 12))
    start, end = ts(2026, 10, 31, 22), ts(2026, 11, 1, 6)
    assert end - start == 9 * 3600                       # one hour longer in real time
    assert a.evaluate(start - 1) is False and a.evaluate(start) is True
    assert a.evaluate(ts(2026, 11, 1, 1, 30, fold=0)) is True   # first 01:30 (EDT)
    assert a.evaluate(ts(2026, 11, 1, 1, 30, fold=1)) is True   # second 01:30 (EST)
    assert a.evaluate(end - 1) is True and a.evaluate(end) is False
    # 09:00-17:30 the week after: still 09:00 local, now 14:00 UTC instead of 13:00
    b = with_schedule(KV(), [{"days": WEEKDAYS, "start": "09:00", "end": "17:30"}], ts(2026, 10, 30, 8))
    fri, mon = ts(2026, 10, 30, 9), ts(2026, 11, 2, 9)
    assert datetime.fromtimestamp(fri, timezone.utc).hour == 13 and datetime.fromtimestamp(mon, timezone.utc).hour == 14
    assert b.evaluate(fri) and not b.evaluate(fri - 1) and b.evaluate(mon) and not b.evaluate(mon - 1)
    assert b.public(ts(2026, 10, 30, 18))["next_change"] == {"t": mon, "armed": True}


def test_dst_spring_forward_and_the_skipped_hour():
    kv = KV()
    a = with_schedule(kv, [{"days": [6], "start": "02:30", "end": "05:00"}], ts(2026, 3, 7, 12))  # Sun 8 Mar
    # 02:30 doesn't exist that night: the window starts at 03:30 EDT (= 02:30 at the old offset)
    starts = ts(2026, 3, 8, 3, 30)
    assert a.evaluate(starts - 1) is False and a.evaluate(starts) is True
    assert a.evaluate(ts(2026, 3, 8, 4, 59)) is True and a.evaluate(ts(2026, 3, 8, 5)) is False
    # a window across the change keeps its wall-clock ends and is an hour shorter
    s = Schedule(normalize_windows([{"days": [5], "start": "22:00", "end": "06:00"}]), NY)
    assert s.armed_at(ts(2026, 3, 8, 5, 59)) and not s.armed_at(ts(2026, 3, 8, 6))
    assert ts(2026, 3, 8, 6) - ts(2026, 3, 7, 22) == 7 * 3600


def test_manual_override_wins_until_the_next_boundary():
    kv = KV()
    a = with_schedule(kv, [{"days": WEEKDAYS, "start": "09:00", "end": "17:30"}], ts(2026, 11, 23, 8))
    home_early = ts(2026, 11, 23, 13)
    assert a.evaluate(home_early) is True
    a.set_manual(False, home_early)                       # came home early: disarm
    p = a.public(home_early + 60)
    assert p["armed"] is False and p["mode"] == "manual" and p["override_until"] == ts(2026, 11, 23, 17, 30)
    assert p["next_change"] == {"t": ts(2026, 11, 24, 9), "armed": True}
    assert a.evaluate(ts(2026, 11, 23, 17, 29)) is False and a.mode(ts(2026, 11, 23, 17, 29)) == "manual"
    assert a.evaluate(ts(2026, 11, 23, 17, 30)) is False and a.mode(ts(2026, 11, 23, 17, 30)) == "schedule"
    assert kv.kv_get("arm_manual") is None                # the expired override is dropped (and persisted)
    assert a.evaluate(ts(2026, 11, 24, 9)) is True
    # manual arm in the evening holds until the next morning's boundary, then the schedule governs
    eve = ts(2026, 11, 24, 20)
    a.set_manual(True, eve, 0)
    assert a.evaluate(ts(2026, 11, 25, 8, 59)) is True and a.mode(ts(2026, 11, 25, 8, 59)) == "manual"
    assert a.evaluate(ts(2026, 11, 25, 9)) is True and a.mode(ts(2026, 11, 25, 9)) == "schedule"
    assert a.evaluate(ts(2026, 11, 25, 17, 30)) is False


def test_restart_mid_window():
    kv = KV()
    win = [{"days": WEEKDAYS, "start": "09:00", "end": "17:30"}]
    a = with_schedule(kv, win, ts(2026, 11, 23, 8))
    a.set_manual(False, ts(2026, 11, 23, 10))             # manual disarm inside the window
    # restart at 11:00: still disarmed (override holds until 17:30)
    b = Arming(kv, 60, tz=NY)
    assert b.schedule_on and b.evaluate(ts(2026, 11, 23, 11)) is False and b.mode(ts(2026, 11, 23, 11)) == "manual"
    # engine down from 11:00 to the next day 10:00: the 17:30 boundary passed while down -> schedule again
    c = Arming(kv, 60, tz=NY)
    assert c.evaluate(ts(2026, 11, 24, 10)) is True and c.mode(ts(2026, 11, 24, 10)) == "schedule"
    # plain schedule, restart mid-window: armed straight away
    d = Arming(kv, 60, tz=NY)
    assert d.evaluate(ts(2026, 11, 24, 12)) is True


def test_saving_the_schedule_never_flips_the_current_state():
    kv = KV()
    a = Arming(kv, 60, tz=NY)
    now = ts(2026, 11, 23, 10)                              # Monday, inside the new window
    a.set_settings(now, schedule={"enabled": True, "windows": [{"days": WEEKDAYS, "start": "09:00", "end": "17:30"}]})
    assert a.evaluate(now) is False and a.mode(now) == "manual"
    assert a.public(now)["next_change"] == {"t": ts(2026, 11, 24, 9), "armed": True}
    # turning it off keeps the current state too
    later = ts(2026, 11, 24, 10)
    assert a.evaluate(later) is True
    a.set_settings(later, schedule={"enabled": False})
    assert a.evaluate(later + 86400) is True and a.mode(later) == "manual"


def test_validation():
    with pytest.raises(ValueError):
        normalize_windows([{"days": [], "start": "09:00", "end": "10:00"}])
    with pytest.raises(ValueError):
        normalize_windows([{"days": [7], "start": "09:00", "end": "10:00"}])
    with pytest.raises(ValueError):
        normalize_windows([{"days": [1], "start": "9am", "end": "10:00"}])
    with pytest.raises(ValueError):
        normalize_windows([{"days": [1], "start": "24:00", "end": "10:00"}])
    assert normalize_windows([{"days": [3, 1, 1], "start": "9:05", "end": "24:00"}]) == \
        [{"days": [1, 3], "start": "09:05", "end": "24:00"}]
    a = Arming(KV(), 60, tz=NY)
    with pytest.raises(ValueError):
        a.set_settings(0, schedule={"enabled": True, "windows": []})
    with pytest.raises(ValueError):
        a.set_settings(0, delay_s=-1)
    assert local_zone() is not None


# ---------------------------------------------------------------- through the engine + API
def test_api_arm_delay_logs_without_alerting(tmp_path, idle_rows):
    async def run():
        eng = make_engine(tmp_path)
        c = TestClient(TestServer(create_app(eng)), headers={"Authorization": "Bearer " + eng.token_path.read_text().strip()})
        await c.start_server()
        try:
            s = await (await c.post("/api/arm", json={"armed": True, "delay_s": 600})).json()
            assert s["armed"] is False and s["arm"]["mode"] == "delay" and s["arm"]["target"] is True
            assert s["arm"]["arm_at"] > time.time() + 590
            assert (await c.post("/api/arm", json={"armed": True, "delay_s": "soon"})).status == 400
            rows, shift = scenario(idle_rows)
            feed(eng, rows, shift)                       # the walk at +150 s happens during the delay
            evs = (await (await c.get("/api/events")).json())["events"]
            assert evs and all(e["armed"] is False for e in evs)
            assert eng.router.results == {}
            s = await (await c.post("/api/arm", json={"armed": False})).json()
            assert s["arm"]["mode"] == "manual" and s["arm"]["target"] is False
            # the 0.2 contract: no delay_s arms at once
            s = await (await c.post("/api/arm", json={"armed": True})).json()
            assert s["armed"] is True and s["arm"]["mode"] == "manual"
        finally:
            await c.close()
            await eng.stop()
    asyncio.run(run())


def test_api_arming_settings_and_tick_driven_schedule(tmp_path):
    async def run():
        eng = make_engine(tmp_path)
        c = TestClient(TestServer(create_app(eng)), headers={"Authorization": "Bearer " + eng.token_path.read_text().strip()})
        await c.start_server()
        try:
            v = await (await c.get("/api/arming")).json()
            assert v["settings"] == {"delay_s": 60, "schedule": {"enabled": False, "windows": []}}
            assert v["timezone"]
            bad = await c.post("/api/arming", json={"schedule": {"enabled": True, "windows": [{"days": [9]}]}})
            assert bad.status == 400 and "days" in (await bad.json())["error"]
            assert (await c.post("/api/arming", json={"nope": 1})).status == 400
            v = await (await c.post("/api/arming", json={"delay_s": 30, "schedule": {
                "enabled": True, "windows": [{"days": list(range(7)), "start": "00:00", "end": "24:00"}]}})).json()
            assert v["settings"]["delay_s"] == 30 and v["state"]["schedule_on"] is True
            # saving held the current (disarmed) state; the always-on window has its next boundary at midnight
            assert v["state"]["armed"] is False and v["state"]["mode"] == "manual"
            # once that boundary has passed, a tick hands control to the schedule
            eng.arming.manual["t"] -= 2 * 86400
            eng.tick(float(int(time.time())))
            s = await (await c.get("/api/status")).json()
            assert s["armed"] is True and s["arm"]["mode"] == "schedule" and eng.detector.armed is True
            # remote clients need the token for the new endpoints too
            for path in ("/api/arming", "/api/alerts", "/api/onboarding"):
                assert (await c.get(path, headers={"Host": "100.64.10.20:8450", "Authorization": ""})).status == 401
            r = await c.post("/api/arming", json={"delay_s": 0}, headers={"Origin": "http://evil.example"})
            assert r.status == 403
        finally:
            await c.close()
            await eng.stop()
    asyncio.run(run())
