"""Detection core: sources -> per-source scores -> fused score -> events.

Pure and clock-free: time only comes in through sample timestamps and tick(t),
so the live engine, the replay tool and the tests run exactly this code.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import __version__ as VERSION
from .baseline import RobustBaseline
from .config import LEVELS
from .features import Esp32Scorer, MacScorer


def would_fire(scores, thr: float, off: float, need: int) -> bool:
    """Same opening rule as the live state machine: a run that starts >= thr, survives dips
    down to off, and collects `need` ticks >= thr."""
    n, active = 0, False
    for s in scores:
        if s >= thr or (active and s >= off):
            active = True
            if s >= thr:
                n += 1
                if n >= need:
                    return True
        else:
            active, n = False, 0
    return False


class MemoryStore:
    """Event store used by replay/tests (the live engine uses storage.Storage)."""

    def __init__(self):
        self.events: dict[int, dict] = {}
        self._next = 1

    def create_event(self, ev: dict) -> int:
        i = self._next
        self._next += 1
        self.events[i] = dict(ev, id=i)
        return i

    def update_event(self, ev: dict) -> None:
        self.events[ev["id"]] = dict(ev)

    def last_event(self) -> dict | None:
        return self.events[max(self.events)] if self.events else None


@dataclass
class SourceState:
    id: str
    kind: str
    scorer: object
    weight: float = 1.0
    baselines: dict = field(default_factory=dict)
    first_seen: float | None = None
    last_seen: float | None = None
    arrivals: deque = field(default_factory=lambda: deque(maxlen=2000))
    online: bool = False
    score: float | None = None
    raw: float | None = None
    key: str | None = None
    factor: float = 1.0
    aux_z: dict = field(default_factory=dict)
    last_scored: float = -1e18

    def baseline(self) -> RobustBaseline | None:
        return self.baselines.get(self.key) if self.key else None

    def rate_hz(self, t: float, span: float = 5.0) -> float:
        n = 0
        for ts in reversed(self.arrivals):
            if ts <= t - span:
                break
            n += 1
        if self.first_seen is not None:
            span = min(span, max(t - self.first_seen, 1e-3))
        return round(n / span, 2)


@dataclass
class TickResult:
    t: float
    state: str
    score: float
    threshold: float
    sources: dict
    emits: list


class Detector:
    def __init__(self, cfg: dict, store=None, tick_s: float = 1.0):
        self.cfg = cfg
        self.tick_s = tick_s
        self.store = store or MemoryStore()
        ev = cfg["detect"]["events"]
        self.thresholds = {k: float(v) for k, v in cfg["detect"]["thresholds"].items()}
        self.sensitivity = ev["sensitivity"]
        self.off_ratio = float(ev["off_ratio"])
        self.min_duration = float(ev["min_duration_s"])
        self.end_timeout = float(ev["end_timeout_s"])
        self.merge_gap = float(ev["merge_gap_s"])
        self.offline_after = float(cfg["sources"]["offline_after_s"])
        self.holdoff = float(cfg["baseline"]["holdoff_s"])
        self.gate_ratio = float(cfg["baseline"]["gate_ratio"])
        aux = cfg["detect"]["aux"]
        self.aux_on = bool(aux["enabled"])
        self.aux_z0, self.aux_k, self.aux_min = float(aux["z0"]), float(aux["k"]), float(aux["min_factor"])
        self.aux_w = {"noise": aux["w_noise"], "cca": aux["w_cca"], "cca_intf": aux["w_cca_intf"],
                      "retrans": aux["w_retrans"]}
        self.weights = {"mac": float(cfg["mac"]["weight"]), "esp32": float(cfg["esp32"]["weight"])}
        self.walk_max = float(cfg["walktest"]["max_duration_s"])
        self.walk_reliable_frac = float(cfg["walktest"]["reliable_fraction"])

        self.sources: dict[str, SourceState] = {}
        self.saved_baselines: dict[str, dict] = {}
        self.armed = False
        self.event: dict | None = None
        self.last_event: dict | None = None
        self.above_since: float | None = None
        self._above_n = 0
        self._cand_peak = 0.0  # highest fused score of the candidate run (it can peak before the event opens)
        self.cand: set = set()
        self.last_event_end: float = -math.inf
        self._last_update_emit = -math.inf
        self.state = "offline"
        self.state_since: float | None = None
        self.started: float | None = None
        self.last_t: float | None = None
        self.last_score = 0.0
        self.idle_scores: deque = deque(maxlen=600)
        self.walk: dict | None = None
        self.walk_result: dict | None = None
        self._pending_emits: list = []
        self._online_snapshot: dict = {}

    def forget_source(self, src: str) -> None:
        """Drop an ESP32 board whose listener slot expired (its name may be reused by another board)."""
        st = self.sources.get(src)
        if st is not None and st.kind != "mac" and not (self.event and src in self.event.get("sources", [])):
            del self.sources[src]
            self._emit("status")

    # ------------------------------------------------------------------ ingest
    def ingest(self, src: str, kind: str, payload: dict, t: float) -> SourceState:
        caps = kind == "mac" and payload.get("type") == "caps"
        st = self.sources.get(src)
        if st is None:
            scorer = MacScorer(self.cfg) if kind == "mac" else Esp32Scorer(self.cfg)
            st = SourceState(id=src, kind=kind, scorer=scorer, weight=self.weights.get(kind, 1.0))
            st.first_seen = t
            saved = self.saved_baselines.pop(src, None)
            if saved and saved.get("kind") == kind:
                for key, bd in saved.get("baselines", {}).items():
                    bl = RobustBaseline(self.cfg, kind, self.tick_s)
                    bl.load_dict(bd)
                    st.baselines[key] = bl
            self.sources[src] = st
        if caps:
            # wifisense's capabilities line (start-up and whenever the link / available signals change):
            # selects the scoring mode; it is not a sample, so it never counts towards rate or online.
            st.scorer.set_caps(payload, t)
            self._online_snapshot = {}  # push a status on the next tick (health / notice may change)
            return st
        st.last_seen = t if st.last_seen is None else max(st.last_seen, t)
        st.arrivals.append(t)
        st.scorer.add(payload, t)
        return st

    # ------------------------------------------------------------------ helpers
    @property
    def threshold(self) -> float:
        return self.thresholds[self.sensitivity]

    def _factor(self, st: SourceState, bl: RobustBaseline) -> float:
        if not self.aux_on or st.kind != "mac":
            st.aux_z = {}
            return 1.0
        z = bl.aux_z(st.scorer.aux)
        st.aux_z = {k: round(v, 2) for k, v in z.items()}
        zw = max((self.aux_w.get(k, 0.0) * v for k, v in z.items()), default=0.0)
        f = 1.0 / (1.0 + self.aux_k * max(0.0, zw - self.aux_z0))
        return max(self.aux_min, f)

    def _emit(self, kind: str, obj=None) -> None:
        self._pending_emits.append((kind, obj))

    def _set_state(self, t: float, s: str) -> None:
        if s != self.state or self.state_since is None:
            self.state = s
            self.state_since = t
            self._emit("status")

    # ------------------------------------------------------------------ tick
    def tick(self, t: float) -> TickResult:
        if self.started is None:
            self.started = t
        self.last_t = t
        thr = self.threshold
        per: dict[str, float | None] = {}
        raws: dict[str, float] = {}
        learned_scores, prov_scores = [], []
        for sid, st in self.sources.items():
            st.online = st.last_seen is not None and (t - st.last_seen) <= self.offline_after
            st.score = None
            st.raw = None
            if not st.online or st.scorer.settling(t) or st.scorer.health(t) is not None:
                per[sid] = None  # offline, settling after a channel change, or blind (no usable signal)
                continue
            r = st.scorer.feature(t)
            key = st.scorer.feature_key(t)
            st.key = key
            if r is None or key is None:
                per[sid] = None
                continue
            bl = st.baselines.get(key)
            if bl is None:
                bl = st.baselines[key] = RobustBaseline(self.cfg, st.kind, self.tick_s)
            raws[sid] = r
            st.raw = r
            st.last_scored = t  # a usable feature this tick (also during learning)
            s = bl.score(r)
            if s is None:
                per[sid] = None
                continue
            st.factor = self._factor(st, bl)
            s = max(0.0, s) * st.factor * st.weight
            st.score = s
            per[sid] = s
            (learned_scores if bl.learned else prov_scores).append((sid, s))

        # "Covered" = some learned source produced a score recently (brief None ticks after a gap or
        # channel change do not flip the state; a blind or silent source does).
        any_learned = bool(learned_scores) or any(
            st.online and st.baseline() is not None and st.baseline().learned and t - st.last_scored <= 10.0
            for st in self.sources.values())
        any_scoring = any(st.online and t - st.last_scored <= 10.0 for st in self.sources.values()) \
            or bool(learned_scores or prov_scores)
        fused = max((s for _, s in learned_scores), default=0.0)
        display = fused if learned_scores else max((s for _, s in prov_scores), default=0.0)

        # ---- event state machine (only on learned sources)
        off = self.off_ratio * thr
        if self.event is None and self.walk is not None:
            # Walk test: never open events (and so never alert); the walk is scored separately.
            self.above_since = None
            self.cand = set()
        elif self.event is None:
            # Candidate run: starts at a tick >= threshold, survives dips down to the off
            # threshold (hysteresis), opens once it has min_duration worth of ticks >= threshold.
            if learned_scores and (fused >= thr or (self.above_since is not None and fused >= off)):
                if self.above_since is None:
                    self.above_since = t
                    self.cand = set()
                    self._above_n = 0
                    self._cand_peak = 0.0
                self._cand_peak = max(self._cand_peak, fused)
                if fused >= thr:
                    self._above_n += 1
                    self.cand |= {sid for sid, s in learned_scores if s >= thr}
                if self._above_n * self.tick_s >= self.min_duration - 1e-9:
                    self._open(t, fused)
            else:
                self.above_since = None
                self.cand = set()
        else:
            ev = self.event
            if learned_scores and fused >= off:
                ev["last_active"] = t
                changed = False
                if fused > ev["peak_score"]:
                    ev["peak_score"] = fused
                    ev["threshold"] = thr
                    changed = True
                new = {sid for sid, s in learned_scores if s >= off} - set(ev["sources"])
                if new:
                    ev["sources"] = sorted(set(ev["sources"]) | new)
                    changed = True
                if changed and t - self._last_update_emit >= 1.0 or t - self._last_update_emit >= 5.0:
                    self._update_event(t)
            elif t - ev["last_active"] >= self.end_timeout or not any_learned:
                self._close(t)

        # ---- baseline maintenance
        quiet = (self.event is None and self.above_since is None and self.walk is None
                 and t - self.last_event_end >= self.holdoff)
        gate = self.gate_ratio * thr
        for sid, r in raws.items():
            st = self.sources[sid]
            bl = st.baselines[st.key]
            if self.walk is not None:  # walk-test ticks never feed a baseline
                continue
            s = per.get(sid)
            elevated = s is not None and s >= gate
            res = bl.update(t, r, {} if st.kind != "mac" else dict(st.scorer.aux), quiet and not elevated, elevated)
            if res == "learned":
                self._emit("status")
            elif res == "absorbed":
                self._emit("absorbed", {"source": sid, "t": t})
                if self.event is not None and sid in self.event["sources"]:
                    note = self.event.get("note") or ""
                    msg = f"steady disturbance on {sid} absorbed into baseline"
                    if msg not in note:
                        self.event["note"] = (note + "; " if note else "") + msg
                    self.event["absorbed"] = True

        if quiet and learned_scores:
            self.idle_scores.append(fused)

        # ---- walk test
        if self.walk is not None:
            self.walk["scores"].append(fused if learned_scores else display)
            for sid, s in per.items():
                if s is not None:
                    self.walk["per_source"].setdefault(sid, []).append(s)
            if t - self.walk["started"] >= self.walk_max:
                self.walktest_stop(t)

        # ---- state + online changes
        # push a status when a source comes/goes or its health message changes (the dashboard shows both)
        online_now = {sid: (st.online, st.scorer.health(t) if st.online else None,
                            st.scorer.notice() if hasattr(st.scorer, "notice") else None)
                      for sid, st in self.sources.items()}
        if online_now != self._online_snapshot:
            self._online_snapshot = online_now
            self._emit("status")
        if not any(st.online for st in self.sources.values()) or (
                not any_scoring and all(st.first_seen is not None and t - st.first_seen > 15
                                        for st in self.sources.values())):
            s = "offline"
        elif not any_learned:
            s = "learning"
        elif self.event is not None:
            s = "motion"
        else:
            s = "clear"
        self._set_state(t, s)
        self.last_score = display
        emits, self._pending_emits = self._pending_emits, []
        return TickResult(t, self.state, display, thr, per, emits)

    # ------------------------------------------------------------------ events
    def _public(self, ev: dict, now: float | None = None) -> dict:
        end = ev.get("end")
        ref = end if end is not None else (now if now is not None else self.last_t or ev["start"])
        return {
            "id": ev["id"],
            "start": round(ev["start"], 3),
            "end": None if end is None else round(end, 3),
            "duration_s": round(max(0.0, ref - ev["start"]), 1),
            "peak_score": round(ev["peak_score"], 3),
            "threshold": float(ev.get("threshold", self.threshold)),
            "sources": list(ev["sources"]),
            "armed": bool(ev["armed"]),
            "label": ev.get("label"),
            "note": ev.get("note") or "",
            "walktest": bool(ev.get("walktest")),
        }

    def _open(self, t: float, fused: float) -> None:
        start = self.above_since
        fused = max(fused, self._cand_peak)  # the run's peak, not just the opening tick's score
        le = self.last_event
        if le is not None and le.get("end") is not None and start - le["end"] <= self.merge_gap \
                and not le.get("absorbed") and bool(le["armed"]) == self.armed \
                and bool(le.get("walktest")) == (self.walk is not None):
            ev = le
            ev["end"] = None
            ev["last_active"] = t
            if fused > ev["peak_score"]:
                ev["peak_score"] = fused
                ev["threshold"] = self.threshold
            ev["sources"] = sorted(set(ev["sources"]) | self.cand)
            ev["merged"] = ev.get("merged", 0) + 1
            self.event = ev
            self.store.update_event(self._public(ev, t))
            self._last_update_emit = t
            self._emit("event_update", self._public(ev, t))
        else:
            ev = {
                "start": start,
                "end": None,
                "peak_score": fused,
                "threshold": self.threshold,
                "sources": sorted(self.cand),
                "armed": self.armed,
                "label": None,
                "note": "walk test" if self.walk is not None else "",
                "walktest": self.walk is not None,
                "last_active": t,
            }
            ev["id"] = None
            ev["id"] = self.store.create_event(self._public(ev, t))
            self.event = ev
            self._last_update_emit = t
            self._emit("event_open", self._public(ev, t))
        self.above_since = None
        self.cand = set()

    def _update_event(self, t: float) -> None:
        self._last_update_emit = t
        pub = self._public(self.event, t)
        self.store.update_event(pub)
        self._emit("event_update", pub)

    def _close(self, t: float, note: str | None = None) -> None:
        ev = self.event
        ev["end"] = max(ev["last_active"], ev["start"])
        if note:
            ev["note"] = (ev.get("note") + "; " if ev.get("note") else "") + note
        pub = self._public(ev, t)
        self.store.update_event(pub)
        self._emit("event_close", pub)
        self.last_event = ev
        self.last_event_end = ev["end"]
        self.event = None

    def label_event(self, ev_pub: dict) -> None:
        """Keep the in-memory copy in sync after the API labels an event."""
        for ev in (self.event, self.last_event):
            if ev is not None and ev.get("id") == ev_pub["id"]:
                ev["label"] = ev_pub.get("label")
                ev["note"] = ev_pub.get("note") or ""

    # ------------------------------------------------------------------ controls
    def set_armed(self, armed: bool) -> None:
        if armed != self.armed:
            self.armed = armed
            self._emit("status")

    def set_sensitivity(self, level: str) -> None:
        if level not in LEVELS:
            raise ValueError("level must be low|medium|high")
        if level != self.sensitivity:
            self.sensitivity = level
            self._emit("status")

    def relearn(self, t: float) -> None:
        if self.event is not None:
            self._close(t, "closed by relearn")
        self.last_event = None
        self.above_since = None
        self.cand = set()
        self.idle_scores.clear()
        self.saved_baselines.clear()
        for st in self.sources.values():
            for bl in st.baselines.values():
                bl.relearn()
            st.baselines.clear()
        self._set_state(t, "learning" if any(st.online for st in self.sources.values()) else "offline")

    def drain_emits(self) -> list:
        e, self._pending_emits = self._pending_emits, []
        return e

    # ------------------------------------------------------------------ walk test
    def walktest_start(self, t: float) -> dict:
        if self.walk is None:
            if self.event is not None:
                self._close(t, "closed: walk test started")
            self.above_since = None
            self.cand = set()
            self.walk = {"started": t, "scores": [], "per_source": {}}
            self._emit("status")
        return self.walktest_state()

    def walktest_stop(self, t: float) -> dict:
        if self.walk is None:
            return self.walktest_state()
        w = self.walk
        self.walk = None
        self.last_event_end = max(self.last_event_end, t)  # hold-off: the walker is still around
        self.walk_result = self._walk_result(w, t)
        self._emit("status")
        self._emit("walktest", self.walktest_state())
        return self.walktest_state()

    def walktest_state(self) -> dict:
        if self.walk is not None:
            sc = self.walk["scores"]
            return {
                "running": True,
                "started": round(self.walk["started"], 3),
                "result": None,
                "finished": None,
                "live": {
                    "elapsed_s": round(max(0.0, (self.last_t or self.walk["started"]) - self.walk["started"]), 1),
                    "peak_score": round(max(sc), 3) if sc else 0.0,
                    "median_score": round(float(np.median(sc)), 3) if sc else 0.0,
                },
            }
        r = self.walk_result
        return {"running": False, "started": r["started"] if r else None, "result": r,
                "finished": r.get("finished") if r else None}

    def _walk_result(self, w: dict, t: float) -> dict:
        sc = np.asarray(w["scores"], dtype=float)
        dur = t - w["started"]
        need = max(1, int(math.ceil(self.min_duration / self.tick_s - 1e-9)))
        if len(self.idle_scores) >= 30:
            idle = np.asarray(self.idle_scores)
            idle_med, idle_p99 = float(np.median(idle)), float(np.percentile(idle, 99))
        else:
            idle_med, idle_p99 = None, None
        det, frac = {}, {}
        for lv in LEVELS:
            thr = self.thresholds[lv]
            above = sc >= thr if len(sc) else np.zeros(0, bool)
            det[lv] = would_fire(sc, thr, self.off_ratio * thr, need)
            frac[lv] = float(above.mean()) if len(sc) else 0.0
        # Recommend the LEAST sensitive level that reliably caught the walk (fewest false alarms):
        # it fired, and the walk spent >= 20 % of its ticks above that level's threshold.
        reliable = {lv: det[lv] and frac[lv] >= self.walk_reliable_frac for lv in LEVELS}
        rec = next((lv for lv in LEVELS if reliable[lv]), "none")
        peak = float(sc.max()) if len(sc) else 0.0
        med = float(np.median(sc)) if len(sc) else 0.0
        msgs = []
        if dur < 15:
            msgs.append(f"Walk was only {dur:.0f} s; 30-60 s gives a more reliable result.")
        if idle_med is None:
            msgs.append("The idle baseline is not established yet (still learning or too little quiet "
                        "time), so this result is provisional.")
        fired = [lv for lv in LEVELS if det[lv]]
        if fired:
            msgs.append(f"Walking peaked at {peak:.1f} (median {med:.1f}); it would have triggered at: "
                        + ", ".join(fired) + ".")
        else:
            msgs.append(f"Walking peaked at {peak:.1f} (median {med:.1f}), below every threshold "
                        f"(high = {self.thresholds['high']:.1f}). Walk closer to the Wi-Fi path between "
                        "this Mac and the router, or add an ESP32 board.")
        if rec != "none":
            msgs.append(f"Recommended sensitivity: {rec} (the least sensitive level that reliably caught "
                        "the walk, which keeps false alarms lowest).")
        elif fired:
            msgs.append("Detection was marginal (only brief moments crossed the threshold), so no level is "
                        "recommended; walk closer to the Mac-router path or add an ESP32 board.")
        if idle_p99 is not None:
            msgs.append(f"Idle score: median {idle_med:.2f}, 99th percentile {idle_p99:.2f}.")
        return {
            "started": round(w["started"], 3),
            "duration_s": round(dur, 1),
            "peak_score": round(peak, 3),
            "median_score": round(med, 3),
            "idle_median": round(idle_med, 3) if idle_med is not None else None,  # None = not known yet
            "idle_p99": round(idle_p99, 3) if idle_p99 is not None else None,
            "threshold_by_level": {lv: self.thresholds[lv] for lv in LEVELS},
            "detected_by_level": det,
            "fraction_above_by_level": {lv: round(frac[lv], 3) for lv in LEVELS},
            "per_source_peak": {k: round(max(v), 3) for k, v in w["per_source"].items() if v},
            "recommended": rec,
            "message": " ".join(msgs),
            "n_ticks": int(len(sc)),
            "finished": round(t, 3),
        }

    # ------------------------------------------------------------------ status / persistence
    def learning_progress(self) -> float:
        ps = [st.baseline().progress for st in self.sources.values()
              if st.online and st.baseline() is not None]
        if not ps:
            return 0.0
        return round(max(ps), 3)

    def learning_eta(self) -> float | None:
        """Seconds until the first online source finishes learning (None when not learning)."""
        if self.state != "learning":
            return None
        etas = []
        for st in self.sources.values():
            if not st.online:
                continue
            bl = st.baseline()
            if bl is None:
                etas.append(float(self.cfg["baseline"]["learning_s"]))
            else:
                etas.append(max(0.0, (bl.learning_ticks - bl.n_learn) * self.tick_s))
        return round(min(etas), 1) if etas else None

    def status(self, now: float) -> dict:
        srcs = []
        for sid, st in sorted(self.sources.items()):
            bl = st.baseline()
            d = {
                "id": sid,
                "kind": st.kind,
                "online": bool(st.online),
                "rate_hz": st.rate_hz(now),
                "last_seen": round(st.last_seen, 3) if st.last_seen else None,
                "score": 0.0 if st.score is None else round(st.score, 3),
                "rssi": None if st.scorer.rssi is None else round(st.scorer.rssi, 1),
                "learning": not (bl is not None and bl.learned),
                "learning_progress": round(bl.progress, 3) if bl is not None else 0.0,
                "health": st.scorer.health(now),
                "aux_factor": round(st.factor, 3),
            }
            d.update(st.scorer.extra())
            srcs.append(d)
        return {
            "sensing": self.sensing(now),
            "state": self.state,
            "armed": self.armed,
            "since": round(self.state_since if self.state_since is not None else now, 3),
            "score": round(self.last_score, 3),
            "threshold": self.threshold,
            "sensitivity": self.sensitivity,
            "learning_progress": 1.0 if self.state in ("clear", "motion") else self.learning_progress(),
            "learning_eta_s": self.learning_eta(),
            "sources": srcs,
            "uptime_s": round(now - self.started, 1) if self.started is not None else 0.0,
            "version": VERSION,
            "thresholds": dict(self.thresholds),
            "walktest_running": self.walk is not None,
            "event_id": self.event["id"] if self.event else None,
        }

    def sensing(self, now: float) -> str:
        """How well the online sources can sense: full | reduced | degraded | none."""
        levels = []
        for st in self.sources.values():
            if not st.online or st.scorer.health(now) is not None:
                continue
            n = st.scorer.notice() if hasattr(st.scorer, "notice") else None
            levels.append(n[0] if n else "full")
        for lv in ("full", "reduced", "degraded"):
            if lv in levels:
                return lv
        return "none"

    def export_baselines(self) -> dict:
        out = dict(self.saved_baselines)
        for sid, st in self.sources.items():
            if st.baselines:
                out[sid] = {"kind": st.kind,
                            "baselines": {k: bl.to_dict() for k, bl in st.baselines.items()}}
        return out

    def import_baselines(self, d: dict) -> None:
        self.saved_baselines = dict(d or {})

    def debug(self) -> dict:
        out = {}
        for sid, st in self.sources.items():
            out[sid] = {
                "raw": st.raw,
                "aux_z": st.aux_z,
                "baselines": {k: bl.summary() for k, bl in st.baselines.items()},
            }
        return out
