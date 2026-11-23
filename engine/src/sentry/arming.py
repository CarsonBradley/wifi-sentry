"""Arming: manual arm/disarm with an exit delay, plus an optional weekly schedule.

Rules
  * Alerts go out only while *armed*. Events are always logged; while disarmed (including during the
    exit delay) they are logged as disarmed and never sent anywhere.
  * Manual arm can carry an exit delay (`delay_s`): it becomes armed at `arm_at`. Disarming cancels it.
  * The schedule is a list of windows {days, start, end} in the Mac's local wall-clock time ("HH:MM";
    days 0 = Monday .. 6 = Sunday, the day the window starts on). start > end runs past midnight into
    the next day; start == end is a whole day. Armed inside any window.
  * A manual arm/disarm wins until the next schedule boundary (any window start or end); from then on
    the schedule governs again. Without a schedule the manual setting simply stays.
  * Saving the schedule never flips the current state: it takes over at its next boundary.
  * Restarts: the manual setting (with its time and arm_at) is persisted, so after a restart the
    same rules give the same answer (a boundary that passed while the engine was down ends a
    manual override; an exit delay that ran out while down is armed).

Time zone and DST: boundaries are computed per calendar date from wall-clock times with zoneinfo, so
09:00 stays 09:00 on both sides of a DST change and a night window keeps its wall-clock ends (it is an
hour longer or shorter in real time that night). A boundary inside the skipped spring-forward hour
happens one hour later on the wall clock (02:30 -> 03:30, the same instant as 02:30 at the old offset);
one inside the repeated autumn hour happens at its first occurrence.
"""
from __future__ import annotations

import bisect
import os
import re
import time
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_WINDOWS = 14
MAX_DELAY_S = 3600
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def local_zone() -> tzinfo:
    """The Mac's time zone as a zoneinfo zone (DST-aware): $TZ, else the /etc/localtime link."""
    name = (os.environ.get("TZ") or "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except Exception:  # noqa: BLE001
        pass
    return datetime.now().astimezone().tzinfo  # fixed offset: last resort, DST-blind


def parse_hhmm(s, allow_24: bool = False) -> int:
    if allow_24 and s == "24:00":
        return 1440
    m = _HHMM.match(s) if isinstance(s, str) else None
    if not m:
        raise ValueError(f"time {s!r} must be HH:MM (24-hour)")
    return int(m.group(1)) * 60 + int(m.group(2))


def normalize_windows(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("schedule windows must be a list")
    if len(raw) > MAX_WINDOWS:
        raise ValueError(f"at most {MAX_WINDOWS} schedule windows")
    out = []
    for i, w in enumerate(raw):
        if not isinstance(w, dict):
            raise ValueError(f"schedule window {i + 1} must be an object")
        days = w.get("days")
        if not isinstance(days, list) or not days or not all(
                isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
            raise ValueError(f"schedule window {i + 1}: days must be a non-empty list of 0 (Mon) .. 6 (Sun)")
        s = parse_hhmm(w.get("start"))
        e = parse_hhmm(w.get("end"), allow_24=True)
        out.append({"days": sorted(set(days)), "start": f"{s // 60:02d}:{s % 60:02d}",
                    "end": "24:00" if e == 1440 else f"{e // 60:02d}:{e % 60:02d}"})
    return out


def _at(d: date, minutes: int, tz: tzinfo) -> float:
    if minutes >= 1440:
        d, minutes = d + timedelta(days=1), minutes - 1440
    return datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=tz).timestamp()


def occurrences(windows: list[dict], d0: date, d1: date, tz: tzinfo) -> list[tuple[float, float]]:
    """Concrete [start, end) intervals (epoch seconds) of windows that start on dates d0..d1."""
    out = []
    d = d0
    while d <= d1:
        wd = d.weekday()
        for w in windows:
            if wd not in w["days"]:
                continue
            s, e = parse_hhmm(w["start"]), parse_hhmm(w["end"], allow_24=True)
            start = _at(d, s, tz)
            end = _at(d if e > s else d + timedelta(days=1), e, tz)
            if end > start:
                out.append((start, end))
        d += timedelta(days=1)
    return out


class Schedule:
    """Window evaluation with a cache of the surrounding weeks' concrete intervals."""

    def __init__(self, windows: list[dict], tz: tzinfo):
        self.windows = windows
        self.tz = tz
        self._lo = self._hi = None
        self._occ: list[tuple[float, float]] = []
        self._bounds: list[float] = []

    def _ensure(self, t: float) -> None:
        if self._lo is not None and self._lo <= t < self._hi:
            return
        d = datetime.fromtimestamp(t, self.tz).date()
        self._occ = occurrences(self.windows, d - timedelta(days=9), d + timedelta(days=17), self.tz)
        self._bounds = sorted({x for o in self._occ for x in o})
        self._lo = _at(d - timedelta(days=1), 0, self.tz)
        self._hi = _at(d + timedelta(days=8), 0, self.tz)

    def armed_at(self, t: float) -> bool:
        self._ensure(t)
        return any(s <= t < e for s, e in self._occ)

    def last_boundary(self, t: float) -> float | None:
        """Latest window start or end at or before t (within the last week)."""
        self._ensure(t)
        i = bisect.bisect_right(self._bounds, t)
        return self._bounds[i - 1] if i else None

    def next_boundary(self, t: float) -> float | None:
        """Earliest window start or end after t (within the next week)."""
        self._ensure(t)
        i = bisect.bisect_right(self._bounds, t)
        return self._bounds[i] if i < len(self._bounds) else None


class Arming:
    """Arm state persisted in the engine's key/value store (kv "arming" = settings, "arm_manual" = the
    last manual arm/disarm). `store` needs kv_get / kv_set / kv_updated (storage.Storage)."""

    def __init__(self, store, default_delay_s: float = 60.0, tz: tzinfo | None = None):
        self.store = store
        self.tz = tz or local_zone()
        cfg = store.kv_get("arming") or {}
        self.delay_s = int(cfg.get("delay_s", default_delay_s))
        sch = cfg.get("schedule") or {}
        self.schedule_enabled = bool(sch.get("enabled"))
        try:
            self.windows = normalize_windows(sch.get("windows") or [])
        except ValueError:
            self.windows = []
        self.sched = Schedule(self.windows, self.tz)
        self.manual: dict | None = store.kv_get("arm_manual")
        if not isinstance(self.manual, dict):
            self.manual = None
            legacy = store.kv_get("armed")  # 0.2.0 and earlier kept only this flag
            if legacy is not None:
                self.manual = {"armed": bool(legacy), "t": store.kv_updated("armed") or time.time(), "arm_at": None}
                store.kv_set("arm_manual", self.manual)

    # ------------------------------------------------------------------ evaluation
    @property
    def schedule_on(self) -> bool:
        return self.schedule_enabled and bool(self.windows)

    def _manual_valid(self, t: float, manual: dict | None) -> dict | None:
        if manual is None or not self.schedule_on:
            return manual
        lb = self.sched.last_boundary(t)
        return None if lb is not None and lb > manual["t"] else manual

    def _state(self, t: float, manual: dict | None) -> tuple[bool, str, bool]:
        """(armed, mode, target) at t. mode: manual | schedule | delay."""
        m = self._manual_valid(t, manual)
        if m is not None:
            if m["armed"] and m.get("arm_at") and t < m["arm_at"]:
                return False, "delay", True
            return bool(m["armed"]), "manual", bool(m["armed"])
        if self.schedule_on:
            a = self.sched.armed_at(t)
            return a, "schedule", a
        return False, "manual", False

    def evaluate(self, t: float) -> bool:
        """Effective armed state at t. Drops (and persists dropping) an expired manual override."""
        if self.manual is not None and self._manual_valid(t, self.manual) is None:
            self.manual = None
            self.store.kv_set("arm_manual", None)
        return self._state(t, self.manual)[0]

    def mode(self, t: float) -> str:
        return self._state(t, self.manual)[1]

    def next_change(self, t: float) -> dict | None:
        """When the effective armed state next flips (within a week), and to what."""
        cur = self._state(t, self.manual)[0]
        cands = []
        if self.manual and self.manual.get("arm_at") and self.manual["arm_at"] > t:
            cands.append(self.manual["arm_at"])
        if self.schedule_on:
            b = t
            for _ in range(4 * MAX_WINDOWS * 8):
                b = self.sched.next_boundary(b)
                if b is None or b > t + 8 * 86400:
                    break
                cands.append(b)
        for c in sorted(cands):
            if self._state(c, self.manual)[0] != cur:
                return {"t": round(c, 3), "armed": not cur}
        return None

    def public(self, t: float) -> dict:
        self.evaluate(t)
        armed, mode, target = self._state(t, self.manual)
        m = self.manual
        return {
            "armed": armed,
            "mode": mode,              # why: manual | schedule | delay (exit delay running)
            "target": target,          # what it is heading to (True during the exit delay)
            "arm_at": round(m["arm_at"], 3) if mode == "delay" and m else None,
            "delay_s": self.delay_s,   # configured exit delay
            "schedule_on": self.schedule_on,
            "scheduled": self.sched.armed_at(t) if self.schedule_on else None,
            # a manual setting lasts until the next schedule boundary
            "override_until": (self.sched.next_boundary(t) if self.schedule_on and m is not None else None),
            "next_change": self.next_change(t),
        }

    def settings(self) -> dict:
        return {"delay_s": self.delay_s,
                "schedule": {"enabled": self.schedule_enabled, "windows": [dict(w) for w in self.windows]}}

    # ------------------------------------------------------------------ changes
    def set_manual(self, armed: bool, t: float, delay_s: float | None = None) -> None:
        delay = 0.0 if delay_s is None else float(delay_s)
        if not 0 <= delay <= MAX_DELAY_S:
            raise ValueError(f"delay_s must be between 0 and {MAX_DELAY_S}")
        arm_at = None
        if armed:
            now_armed, mode, _ = self._state(t, self.manual)
            if mode == "delay":
                arm_at = self.manual["arm_at"]      # arming again doesn't restart the countdown
            elif not now_armed and delay > 0:
                arm_at = t + delay
        self.manual = {"armed": bool(armed), "t": t, "arm_at": arm_at}
        self.store.kv_set("arm_manual", self.manual)

    def set_settings(self, t: float, delay_s=None, schedule=None) -> None:
        if delay_s is not None:
            if isinstance(delay_s, bool) or not isinstance(delay_s, (int, float)) or not 0 <= delay_s <= MAX_DELAY_S:
                raise ValueError(f"delay_s must be a number of seconds between 0 and {MAX_DELAY_S}")
        if schedule is not None:
            if not isinstance(schedule, dict):
                raise ValueError("schedule must be an object {enabled, windows}")
            enabled = schedule.get("enabled", self.schedule_enabled)
            if not isinstance(enabled, bool):
                raise ValueError("schedule.enabled must be a boolean")
            windows = normalize_windows(schedule.get("windows", self.windows))
            if enabled and not windows:
                raise ValueError("add at least one time window to turn the schedule on")
        if delay_s is not None:
            self.delay_s = int(delay_s)
        if schedule is not None:
            # Saving the schedule never flips the current state: hold it as a manual setting until the
            # new schedule's next boundary.
            armed, mode, target = self._state(t, self.manual)
            old_m = self.manual
            self.schedule_enabled = enabled
            self.windows = windows
            self.sched = Schedule(self.windows, self.tz)
            arm_at = old_m.get("arm_at") if (mode == "delay" and old_m) else None
            self.manual = {"armed": bool(target), "t": t, "arm_at": arm_at}
            self.store.kv_set("arm_manual", self.manual)
        self.store.kv_set("arming", self.settings())
