"""Adaptive robust baseline for one feature stream (one source / layout).

score = (r - median) / scale,   scale = max(mad_k * 1.4826 * MAD, rel_floor * median, abs_floor)

* Learning: the first `learning_ticks` ticks with a valid feature are all accepted.
* After learning, a tick is accepted only when the detector says so (no event
  open or pending, outside the post-event hold-off, not in a walk test, and this
  source's own score below gate). The median/MAD are recomputed over the last
  `window` accepted ticks every `recompute_every` ticks, so slow drift is
  tracked but anything that looks like motion is never learned.
* Bounded absorption: a source continuously rejected for `absorb_after_s` whose
  feature over the last `absorb_window_s` was elevated in >= absorb_min_frac of
  ticks and stationary (robust CV <= absorb_max_cv) has its baseline rebuilt
  from that window. A fan passes; intermittent human movement does not.
* Auxiliary baselines (noise, cca, ...) are learned with the same accept rule.
"""
from __future__ import annotations

from collections import deque

import numpy as np

AUX_FLOORS = {"noise": 1.0, "cca": 5.0, "cca_intf": 2.0, "retrans": 0.01}


def _med_mad(vals) -> tuple[float, float]:
    a = np.asarray(vals, dtype=float)
    med = float(np.median(a))
    return med, float(np.median(np.abs(a - med)))


class RobustBaseline:
    def __init__(self, cfg: dict, kind: str, tick_s: float = 1.0):
        b = cfg["baseline"]
        d = cfg["detect"]
        self.tick_s = tick_s
        self.learning_ticks = max(1, int(round(float(b["learning_s"]) / tick_s)))
        self.min_ticks = int(b["min_learning_ticks"])
        self.window = max(10, int(round(float(b["window_s"]) / tick_s)))
        self.recompute_every = max(1, int(round(float(b["recompute_every_s"]) / tick_s)))
        self.absorb_after_s = float(b["absorb_after_s"])
        self.absorb_window = max(10, int(round(float(b["absorb_window_s"]) / tick_s)))
        self.absorb_min_frac = float(b["absorb_min_frac"])
        self.absorb_max_cv = float(b["absorb_max_cv"])
        self.force_after_s = float(b["force_absorb_after_s"])
        self.mad_k = float(d["mad_k"])
        self.abs_floor = float(d[f"{kind}_abs_floor"])
        self.rel_floor = float(d[f"{kind}_rel_floor"])
        self.buf: deque = deque(maxlen=self.window)
        self.aux: dict[str, deque] = {}
        self.aux_stats: dict[str, tuple[float, float]] = {}
        self.recent: deque = deque(maxlen=self.absorb_window)  # (r, elevated)
        self.learned = False
        self.n_learn = 0
        self.median: float | None = None
        self.mad: float | None = None
        self.scale: float | None = None
        self._since_recompute = 0
        self.rejected_since: float | None = None
        self.absorbed = 0

    # ---------------------------------------------------------------------
    @property
    def progress(self) -> float:
        return 1.0 if self.learned else min(1.0, self.n_learn / self.learning_ticks)

    def ready(self) -> bool:
        return self.median is not None

    def score(self, r: float) -> float | None:
        if self.median is None:
            return None
        return (r - self.median) / self.scale

    def _recompute(self) -> None:
        if len(self.buf) < max(3, min(self.min_ticks, self.learning_ticks)):
            return
        self.median, self.mad = _med_mad(self.buf)
        self.scale = max(self.mad_k * 1.4826 * self.mad, self.rel_floor * abs(self.median), self.abs_floor)
        for k, dq in self.aux.items():
            if len(dq) >= 10:
                self.aux_stats[k] = _med_mad(dq)
        self._since_recompute = 0

    def aux_z(self, aux: dict) -> dict[str, float]:
        out = {}
        for k, v in aux.items():
            if v is None or k not in self.aux_stats:
                continue
            med, mad = self.aux_stats[k]
            s = max(1.4826 * mad, AUX_FLOORS.get(k, 1.0))
            # positive = worse: higher noise floor (less negative dBm), busier air, more retries
            out[k] = (v - med) / s
        return out

    # ---------------------------------------------------------------------
    def update(self, t: float, r: float, aux: dict, accept: bool, elevated: bool) -> str | None:
        """Feed one tick. Returns "learned" / "absorbed" / None."""
        if not self.learned:
            self._push(r, aux)
            self.n_learn += 1
            if self.n_learn >= self.min_ticks or self.median is None:
                if self.n_learn % self.recompute_every == 0 or self.median is None or self.n_learn >= self.learning_ticks:
                    self._recompute()
            if self.n_learn >= self.learning_ticks:
                self.learned = True
                self._recompute()
                return "learned"
            return None
        self.recent.append((r, elevated))
        if accept:
            self._push(r, aux)
            self.rejected_since = None
            self._since_recompute += 1
            if self._since_recompute >= self.recompute_every:
                self._recompute()
            return None
        if self.rejected_since is None:
            self.rejected_since = t
        held = t - self.rejected_since
        if held >= self.absorb_after_s and self._stationary_elevated():
            return self._absorb()
        if self.force_after_s > 0 and held >= self.force_after_s:
            return self._absorb()
        return None

    def _push(self, r: float, aux: dict) -> None:
        self.buf.append(r)
        for k, v in aux.items():
            if v is None:
                continue
            dq = self.aux.get(k)
            if dq is None:
                dq = self.aux[k] = deque(maxlen=self.window)
            dq.append(v)

    def _stationary_elevated(self) -> bool:
        if len(self.recent) < self.absorb_window:
            return False
        rs = [r for r, _ in self.recent]
        frac = sum(1 for _, e in self.recent if e) / len(self.recent)
        med, mad = _med_mad(rs)
        cv = 1.4826 * mad / med if med > 0 else float("inf")
        return frac >= self.absorb_min_frac and cv <= self.absorb_max_cv

    def _absorb(self) -> str:
        self.buf.clear()
        for r, _ in self.recent:
            self.buf.append(r)
        self.recent.clear()
        self.rejected_since = None
        self.absorbed += 1
        self._recompute()
        return "absorbed"

    def relearn(self) -> None:
        self.buf.clear()
        self.aux.clear()
        self.aux_stats.clear()
        self.recent.clear()
        self.learned = False
        self.n_learn = 0
        self.median = self.mad = self.scale = None
        self.rejected_since = None

    # ---------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "learned": self.learned,
            "n_learn": self.n_learn,
            "buf": [round(x, 5) for x in self.buf],
            "aux": {k: list(v) for k, v in self.aux.items()},
            "absorbed": self.absorbed,
        }

    def load_dict(self, d: dict) -> None:
        self.buf.clear()
        self.buf.extend(float(x) for x in d.get("buf", []))
        self.aux = {k: deque((float(x) for x in v), maxlen=self.window) for k, v in d.get("aux", {}).items()}
        self.learned = bool(d.get("learned")) and len(self.buf) >= 3
        self.n_learn = self.learning_ticks if self.learned else min(int(d.get("n_learn", 0)), len(self.buf))
        self.absorbed = int(d.get("absorbed", 0))
        self._recompute()

    def summary(self) -> dict:
        return {
            "learned": self.learned,
            "progress": round(self.progress, 3),
            "median": None if self.median is None else round(self.median, 5),
            "mad": None if self.mad is None else round(self.mad, 5),
            "scale": None if self.scale is None else round(self.scale, 5),
            "n": len(self.buf),
            "absorbed": self.absorbed,
        }
