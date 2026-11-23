"""Per-source feature extraction.

Each scorer turns raw samples into one scalar "raw activity" r per 1 Hz tick.
Normalisation against what is normal happens in baseline.py, so r only needs
to grow with motion and be stable when nothing moves.

Mac (wifisense JSONL, 10 Hz): r = V(d) + w * V(c) over the last
mac_window_s seconds, where c = (c0 + c1) / 2 and d = (c0 - c1) / 2 are the
common and differential parts of the two per-chain RSSIs (integer dB), and V
is the step-robust variance (step_robust_var below; plain variance if
mac_step_robust = false). With plain variance and w = 1, r equals the mean of
the two per-chain variances. w < 1 discounts
common-mode changes (AP tx power / beamforming / AGC move both chains
together; a moving body's multipath fades each chain independently).
(Removing exact common-mode jumps was tried and rejected: on the real idle
capture a common-mode zig-zag episode has asymmetric jumps (+2/+2, then
-2/-1), so removing only the matching ones manufactures a fake 2 dB step.)
Idle measurement (10 min on the tested Mac, room still): plain-variance r median 0.21 dB^2,
p99 0.42, max 0.61 (pure 1 dB dither between two levels has variance <= 0.25);
step-robust r median 0.15, MAD 0.03.
Pulse rejection (mac_pulse_reject, common_pulse_mask below): before V is taken,
samples of a short positive common-mode "box" (both chains +3..+4 dB for ~1 s,
then back) are dropped. The first full night (2026-11-23) had 45 such boxes in
3 h on a quiet link and they caused 25 of its 27 false events; a lone step is
one change point, a box is two, so step_robust_var alone could not remove them.

Frozen readings (health): on a quiet link the integer readings can sit on one
value for minutes while frames keep arriving; that is a still room, not a blind
sensor. "Frozen" (blind) now needs the host rx packet counter to have stalled
too; with traffic, only a reading unchanged for mac_stuck_s counts as stuck.

Signal modes (what this Mac's sampler provides, from its {"type":"caps"} line; without one, e.g. old
recordings, two chains are assumed as before):
  * 2 chains ("dual", key "rssi"): the design above, measured on the original Mac.
  * 3-4 chains ("multi", key "rssiN"): c = mean of the chains, r = mean_k V(x_k - c) + w * V(c) (for two
    chains this is exactly V(d) + w * V(c)); the pulse mask needs every pair of chains to stay level.
  * 1 chain ("single", key "rssi1"): r = V(x). The box rejection can't check that "both antennas moved
    together", so it drops ANY short isolated positive box on that chain, and common-mode radio effects
    (AGC, AP power) can't be told apart from a body. Reported as a "reduced" notice.
  * none ("aggregate", key "rssi_agg"): the public, heavily smoothed `rssi` is scored instead. It changes
    every few seconds to minutes, so detection will be poor; reported as "degraded" and never hidden.
Untested on real hardware other than the 2-chain Mac: the 1/3/4-chain and aggregate paths are covered by
unit tests with synthetic sampler output only.
Link state (caps "link"): Wi-Fi off, not associated, or no Wi-Fi interface -> health message, not scored.

ESP32 (CSI datagrams, ~50 Hz): per (src, fmt) group, amplitude |im + j re| per
valid sub-carrier, normalised per frame by the mean amplitude; r = mean over
sub-carriers of (time std / time mean) over the last esp32_window_s seconds.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

AUX_KEYS = ("noise", "cca", "cca_intf", "retrans")


CHAIN_KEYS = ("rssi_c0", "rssi_c1", "rssi_c2", "rssi_c3")

LINK_HEALTH = {
    "off": "Wi-Fi is turned off on this Mac",
    "disconnected": "Wi-Fi is on but not connected to a network",
    "none": "this Mac has no Wi-Fi interface (Ethernet only?)",
}
NOTICE_AGGREGATE = ("degraded: this Mac/macOS doesn't expose per-antenna signal, detection will be poor "
                    "(scoring the smoothed overall signal strength instead)")
NOTICE_SINGLE = ("reduced: this Mac reports only one antenna, so radio artifacts that move every antenna "
                 "together can't be told apart from motion; expect more false alarms")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class MacScorer:
    kind = "mac"

    def __init__(self, cfg: dict):
        d = cfg["detect"]
        s = cfg["sources"]
        self.window_s = float(d["mac_window_s"])
        self.common_w = float(d["mac_common_weight"])
        self.step_robust = bool(d["mac_step_robust"])
        self.pulse_reject = bool(d["mac_pulse_reject"])
        self.pulse_max_s = float(d["mac_pulse_max_s"])
        self.pulse_min_db = float(d["mac_pulse_min_db"])
        self.pulse_max_diff_db = float(d["mac_pulse_max_diff_db"])
        self.pulse_pre_s = PULSE_PRE_S
        # keep enough history before the scoring window to judge a pulse that began before it
        self.keep_s = self.window_s + (self.pulse_max_s + self.pulse_pre_s if self.pulse_reject else 0.0) + 1.0
        self.gap_reset_s = float(s["gap_reset_s"])
        self.settle_s = float(s["channel_settle_s"])
        self.frozen_s = float(s["mac_frozen_s"])
        self.stuck_s = float(s["mac_stuck_s"])
        self._rx_last: int | None = None
        self.last_rx_t: float | None = None  # last time the host rx packet counter advanced
        self.buf: deque = deque()  # (t, v0, v1, ...) in the current mode
        self.last_t: float | None = None
        self.last_raw: tuple | None = None
        self.channel: tuple | None = None
        self.settle_until = 0.0
        self.aux: dict[str, float | None] = {k: None for k in AUX_KEYS}
        self._retx_prev: tuple[float, float] | None = None
        self.rssi: float | None = None
        self.rssi_c0: int | None = None
        self.rssi_c1: int | None = None
        self.chains_now: list[int] | None = None
        self.n_channel_changes = 0
        self.n_gaps = 0
        self.last_change_t: float | None = None
        self.missing_chain_since: float | None = None
        self.caps: dict | None = None     # latest {"type":"caps"} line from wifisense
        self.mode: str | None = None      # dual | multi | single | aggregate | nolink (None: no data yet)
        self.n: int = 0                   # values per sample in the current mode
        self.key: str | None = None       # baseline key of the current mode

    # -- capabilities -------------------------------------------------------
    def set_caps(self, caps: dict, t: float) -> None:
        self.caps = dict(caps)

    def expected(self) -> tuple[str, int]:
        """(mode, values per sample) this Mac's sampler says it provides. Without a caps line (old
        sampler, old recordings) two chains are expected, as before."""
        c = self.caps
        if c is None:
            return "dual", 2
        link = c.get("link")
        if link in LINK_HEALTH:
            return "nolink", 0
        n = c.get("chains") if _num(c.get("chains")) else 0
        n = max(0, min(int(n), len(CHAIN_KEYS)))
        pa = c.get("per_antenna")
        if pa == "ok" and n >= 2:
            return ("dual" if n == 2 else "multi"), n
        if pa == "ok" and n == 1:
            return "single", 1
        if pa == "missing" or (pa == "ok" and n == 0):
            return "aggregate", 1
        return "dual", 2   # link up but the API state is unknown: behave as before

    @staticmethod
    def key_for(mode: str, n: int) -> str | None:
        return {"dual": "rssi", "single": "rssi1", "aggregate": "rssi_agg"}.get(mode, f"rssi{n}" if mode == "multi" else None)

    # -- ingest -------------------------------------------------------------
    def add(self, s: dict, t: float) -> None:
        ch = (s.get("channel"), s.get("chan_width"))
        if ch[0] is not None:
            if self.channel is not None and ch != self.channel:
                self._clear()
                self.settle_until = t + self.settle_s
                self.n_channel_changes += 1
            self.channel = ch
        # slow auxiliaries
        if s.get("noise") is not None:
            self.aux["noise"] = float(s["noise"])
        if s.get("cca") is not None:
            self.aux["cca"] = float(s["cca"])
        if s.get("cca_intf") is not None:
            self.aux["cca_intf"] = float(s["cca_intf"])
        tr, tf = s.get("tx_retrans"), s.get("tx_frames")
        if tr is not None and tf is not None:
            if self._retx_prev is None:
                self._retx_prev = (tr, tf)
            elif tf != self._retx_prev[1]:
                dtf = tf - self._retx_prev[1]
                dtr = tr - self._retx_prev[0]
                if dtf > 0 and dtr >= 0:
                    self.aux["retrans"] = dtr / dtf
                self._retx_prev = (tr, tf)
        rxp = s.get("rx_pkts")
        if isinstance(rxp, (int, float)) and not isinstance(rxp, bool):
            rxp = int(rxp)
            if self._rx_last is not None and rxp != self._rx_last:
                self.last_rx_t = t
            self._rx_last = rxp

        mode, n = self.expected()
        if mode != self.mode or n != self.n:
            self._clear()
            self.mode, self.n = mode, n
            self.key = self.key_for(mode, n)
            self.last_change_t = None
            self.missing_chain_since = None
        if mode == "nolink":
            self.rssi_c0 = self.rssi_c1 = None
            self.chains_now = None
            self.rssi = None
            return
        if mode == "aggregate":
            v = s.get("rssi")
            vals = (int(v),) if _num(v) else None
            chain_vals = None
        else:
            raw = [s.get(k) for k in CHAIN_KEYS[:n]]
            vals = tuple(int(x) for x in raw) if all(_num(x) for x in raw) else None
            chain_vals = vals
        if vals is None:
            if self.missing_chain_since is None:
                self.missing_chain_since = t
            self.rssi_c0 = self.rssi_c1 = None  # don't keep reporting the last values as if current
            self.chains_now = None
            if _num(s.get("rssi")):
                self.rssi = float(s["rssi"])
            return
        self.missing_chain_since = None
        if chain_vals is not None:
            self.chains_now = list(chain_vals)
            self.rssi_c0 = chain_vals[0]
            self.rssi_c1 = chain_vals[1] if len(chain_vals) > 1 else None
            self.rssi = sum(chain_vals) / len(chain_vals)
        else:
            self.chains_now = None
            self.rssi_c0 = self.rssi_c1 = None
            self.rssi = float(vals[0])
        if self.last_t is not None and t - self.last_t > self.gap_reset_s:
            self._clear()
            self.n_gaps += 1
        if self.last_raw is not None:
            if vals != self.last_raw:
                self.last_change_t = t
        elif self.last_change_t is None:
            self.last_change_t = t
        self.last_raw = vals
        self.last_t = t
        self.buf.append((t,) + vals)
        cutoff = t - self.keep_s
        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()

    def _clear(self) -> None:
        self.buf.clear()
        self.last_raw = None

    # -- per tick -----------------------------------------------------------
    def settling(self, t: float) -> bool:
        return t < self.settle_until

    def feature_key(self, t: float) -> str | None:
        return self.key

    def feature(self, t: float) -> float | None:
        lo = t - self.window_s
        rows = [x for x in self.buf if x[0] <= t]
        if not rows:
            return None
        arr = np.asarray(rows, dtype=float)
        inwin = arr[:, 0] > lo
        if int(inwin.sum()) < 8:
            return None
        first = float(arr[inwin, 0][0])
        if first > lo + 0.3 * self.window_s:  # window not covered yet (after gap/start)
            return None
        if self.pulse_reject:
            chains = [arr[:, 1 + k] for k in range(arr.shape[1] - 1)]
            keep = inwin & ~pulse_mask(arr[:, 0], chains, self.pulse_max_s, self.pulse_min_db,
                                       self.pulse_max_diff_db, self.pulse_pre_s)
            if int(keep.sum()) < 8:
                return None
            arr = arr[keep]
        else:
            arr = arr[inwin]
        v = step_robust_var if self.step_robust else np.var
        k = arr.shape[1] - 1
        if k == 1:
            return float(v(arr[:, 1]))
        if k == 2:  # the original two-antenna feature, unchanged
            c = (arr[:, 1] + arr[:, 2]) * 0.5
            d = (arr[:, 1] - arr[:, 2]) * 0.5
            return float(v(d) + self.common_w * v(c))
        x = arr[:, 1:]
        c = x.mean(axis=1)
        return float(np.mean([v(x[:, j] - c) for j in range(k)]) + self.common_w * v(c))

    def health(self, t: float) -> str | None:
        if self.mode == "nolink":
            return LINK_HEALTH.get((self.caps or {}).get("link"), "Wi-Fi not available")
        if self.missing_chain_since is not None and t - self.missing_chain_since > 5:
            if self.mode == "aggregate":
                return "no signal-strength readings from Wi-Fi"
            return "per-chain RSSI unavailable (private API missing?)"
        if self.last_change_t is None:
            return None
        still = t - self.last_change_t
        # An unchanging reading is only "blind" when nothing is being received: on 2026-11-23 the
        # readings sat on one integer value for 15-105 s at a time (113 times in 3 h) while ~80 rx
        # packets/s kept arriving and the value still reacted (it jumped and came back), i.e. a
        # still room, not a stuck sensor. Without a packet counter, fall back to the old 15 s rule.
        traffic = self.last_rx_t is not None and t - self.last_rx_t <= self.frozen_s
        what = "signal strength" if self.mode == "aggregate" else "per-chain RSSI"
        if still > self.frozen_s and not traffic:
            return f"{what} frozen (no link traffic?)"
        # The smoothed aggregate routinely sits on one value for many minutes: not a sign of a fault.
        if still > self.stuck_s and self.mode != "aggregate":
            return "per-chain RSSI unchanged for over %d min despite link traffic (stuck?)" % round(self.stuck_s / 60)
        return None

    def notice(self) -> tuple[str, str] | None:
        """(level, text) for a reduced or degraded signal mode that still scores."""
        if self.mode == "aggregate":
            return "degraded", NOTICE_AGGREGATE
        if self.mode == "single":
            return "reduced", NOTICE_SINGLE
        return None

    def extra(self) -> dict:
        n = self.notice()
        caps = self.caps or {}
        return {
            "rssi_c0": self.rssi_c0,
            "rssi_c1": self.rssi_c1,
            "rssi_chains": self.chains_now,
            "channel": self.channel[0] if self.channel else None,
            "channel_changes": self.n_channel_changes,
            "signal_mode": self.mode,
            "notice": n[1] if n else None,
            "notice_level": n[0] if n else None,
            "link": caps.get("link"),
            "sampler": {k: caps.get(k) for k in ("sampler", "os", "model", "iface", "per_antenna", "chains")}
            if self.caps is not None else None,
        }


class _CsiGroup:
    """One (src, fmt) layout: valid sub-carrier mask + window of normalised amplitudes."""

    MASK_FRAMES = 50

    def __init__(self, n_values: int):
        self.n_sc = n_values // 2
        self.mask: np.ndarray | None = None
        self.pending: list[np.ndarray] = []
        self.buf: deque = deque()  # (t, normalised amplitude vector)

    def add(self, csi: list, t: float, keep_s: float) -> None:
        if len(csi) != self.n_sc * 2:
            return
        c = np.asarray(csi, dtype=np.float32).reshape(-1, 2)
        amp = np.hypot(c[:, 0], c[:, 1])
        if self.mask is None:
            self.pending.append(amp)
            if len(self.pending) >= self.MASK_FRAMES:
                m = np.mean(self.pending, axis=0)
                med = float(np.median(m[m > 0])) if np.any(m > 0) else 0.0
                mask = m > max(0.1 * med, 0.5)
                mask[:2] = False  # slots 0 and 1 are always zeroed by the firmware
                self.mask = mask if mask.sum() >= 8 else None
                self.pending = []
            return
        a = amp[self.mask]
        mean = float(a.mean())
        if mean <= 0:
            return
        self.buf.append((t, a / mean))
        cutoff = t - keep_s
        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()

    def count(self, lo: float, t: float) -> int:
        return sum(1 for ts, _ in self.buf if lo < ts <= t)

    def feature(self, lo: float, t: float) -> float | None:
        rows = [v for ts, v in self.buf if lo < ts <= t]
        if len(rows) < 20:
            return None
        a = np.vstack(rows)
        mu = a.mean(axis=0)
        sd = a.std(axis=0)
        return float(np.mean(sd / np.maximum(mu, 1e-6)))


# A real channel change (router auto-channel, mesh roam) settles once. Datagrams that keep changing it
# (radio trouble, or a spoofer on the unauthenticated port) would keep the board "settling", i.e. silently
# unscored while shown Online: beyond CHANNEL_FLAP_MAX changes per CHANNEL_FLAP_WINDOW_S a change no
# longer extends the settle window and health() reports it instead (which the dashboard shows).
CHANNEL_FLAP_MAX = 3
CHANNEL_FLAP_WINDOW_S = 300.0
HEALTH_CHANNEL_FLAP = "channel keeps changing (possible interference or spoofing)"


class Esp32Scorer:
    kind = "esp32"

    def __init__(self, cfg: dict):
        self.window_s = float(cfg["detect"]["esp32_window_s"])
        self.gap_reset_s = max(float(cfg["sources"]["gap_reset_s"]), 1.0)
        self.settle_s = float(cfg["sources"]["channel_settle_s"])
        self.groups: dict[str, _CsiGroup] = {}
        self.last_seq: int | None = None
        self.last_tus: int | None = None
        self.last_t: float | None = None
        self.frames = 0
        self.lost = 0
        self.reboots = 0
        self.dups = 0
        self.rssi: float | None = None
        self.channel = None
        self.settle_until = 0.0
        self.n_channel_changes = 0
        self.changes: deque = deque(maxlen=CHANNEL_FLAP_MAX + 1)  # times of the latest channel changes
        self.dominant: str | None = None
        self.aux: dict[str, float | None] = {k: None for k in AUX_KEYS}

    def add(self, m: dict, t: float) -> None:
        seq, tus = m.get("seq"), m.get("t_us")
        if isinstance(seq, int) and self.last_seq is not None:
            fwd = (seq - self.last_seq) % (1 << 32)
            if fwd == 0:
                self.dups += 1
                return
            if fwd >= (1 << 31):  # went backwards
                if isinstance(tus, int) and self.last_tus is not None and tus < self.last_tus:
                    self.reboots += 1
                    self._reset()
                else:
                    self.dups += 1
                    return
            else:
                self.lost += fwd - 1
        elif isinstance(tus, int) and self.last_tus is not None and tus < self.last_tus:
            self.reboots += 1
            self._reset()
        if isinstance(seq, int):
            self.last_seq = seq
        if isinstance(tus, int):
            self.last_tus = tus
        if self.last_t is not None and t - self.last_t > self.gap_reset_s:
            for g in self.groups.values():
                g.buf.clear()
        self.last_t = t
        self.frames += 1
        if m.get("rssi") is not None:
            self.rssi = float(m["rssi"])
        ch = m.get("channel")
        if ch is not None:
            if self.channel is not None and ch != self.channel:
                for g in self.groups.values():
                    g.buf.clear()
                    g.mask = None
                self.n_channel_changes += 1
                self.changes.append(t)
                if not self.channel_flapping(t):
                    self.settle_until = t + self.settle_s
            self.channel = ch
        if m.get("noise") is not None:
            self.aux["noise"] = float(m["noise"])
        fmt, csi = m.get("fmt"), m.get("csi")
        if not isinstance(fmt, str) or not isinstance(csi, list) or len(csi) < 16:
            return
        g = self.groups.get(fmt)
        if g is None:
            if len(self.groups) >= 8:
                return
            g = self.groups[fmt] = _CsiGroup(len(csi))
        g.add(csi, t, self.window_s + 1.0)

    def _reset(self) -> None:
        for g in self.groups.values():
            g.buf.clear()
        self.last_seq = None
        self.last_tus = None

    def settling(self, t: float) -> bool:
        return t < self.settle_until

    def channel_flapping(self, t: float) -> bool:
        return len(self.changes) > CHANNEL_FLAP_MAX and t - self.changes[0] < CHANNEL_FLAP_WINDOW_S

    def feature_key(self, t: float) -> str | None:
        lo = t - self.window_s
        best, n = None, 0
        for fmt, g in self.groups.items():
            c = g.count(lo, t)
            if c > n:
                best, n = fmt, c
        self.dominant = best
        return best

    def feature(self, t: float) -> float | None:
        key = self.feature_key(t)
        if key is None:
            return None
        lo = t - self.window_s
        g = self.groups[key]
        rows = [ts for ts, _ in g.buf if lo < ts <= t]
        if not rows or rows[0] > lo + 0.3 * self.window_s:
            return None
        return g.feature(lo, t)

    def health(self, t: float) -> str | None:
        if self.channel_flapping(t):
            return HEALTH_CHANNEL_FLAP
        if self.frames > 100 and all(g.mask is None for g in self.groups.values()) and self.groups:
            return "no usable CSI sub-carriers"
        return None

    def extra(self) -> dict:
        tot = self.frames + self.lost
        return {
            "fmt": self.dominant,
            "channel": self.channel,
            "lost_pct": round(100.0 * self.lost / tot, 2) if tot else 0.0,
            "reboots": self.reboots,
            "channel_changes": self.n_channel_changes,
        }


PULSE_PRE_S = 2.5    # reference level = median of the 2.5 s before a pulse starts
PULSE_PRE_RANGE = 2  # dB: that reference stretch must look idle (per-chain range <= 2 dB)
PULSE_RAMP_N = 4     # samples of rising/falling edge masked on each side of the pulse core


def common_pulse_mask(ts: np.ndarray, c0: np.ndarray, c1: np.ndarray, max_s: float = 2.0,
                      min_db: float = 2.0, max_diff_db: float = 1.0, pre_s: float = PULSE_PRE_S) -> np.ndarray:
    """Two-antenna form of pulse_mask (the measured case)."""
    return pulse_mask(ts, [c0, c1], max_s, min_db, max_diff_db, pre_s)


def pulse_mask(ts: np.ndarray, chains: list, max_s: float = 2.0, min_db: float = 2.0, max_diff_db: float = 1.0,
               pre_s: float = PULSE_PRE_S) -> np.ndarray:
    """Mark samples that belong to a short, rectangular, common-mode, positive RSSI pulse.

    The artifact this removes (2026-11-23 night, 45 of them in 3 h, 25 of the 27 false events):
    both chains jump up together by +3..+4 dB, stay there ~0.6 s, and fall back to the same level
    within ~1-3 s, with c0 - c1 unchanged to within 1 dB the whole time, out of an idle stretch.
    A moving body attenuates more than it boosts, fades the two antennas differently, and keeps
    fluctuating; it does not produce an isolated identical +3 dB box on both chains.

    A pulse = a core run of samples whose common mode (mean of the chains) is >= 1.5 dB above the
    median of the pre_s seconds before the run (plus up to PULSE_RAMP_N edge samples on each side that
    are still >= 0.5 dB above it, because the driver smooths the edges), where
      * the core's peak is >= min_db above that reference (all chains up by about min_db),
      * the difference of every pair of chains never moves more than max_diff_db from its reference
        value (core and edges); with a single chain there is no pair, so any such box qualifies,
      * the reference stretch itself is idle (each chain's range <= PULSE_PRE_RANGE dB), and
      * the core lasts <= max_s (a core that is still going and not yet max_s old counts as a
        pulse for now: it is scored once it outlasts max_s, so a real rise that persists costs
        at most max_s of latency).
    Masked samples are dropped from the variance window. Anything else - negative dips,
    differential changes, long or noisy excursions, steps - is left in and scored as before."""
    n = len(ts)
    mask = np.zeros(n, dtype=bool)
    if n < 4 or not chains:
        return mask
    if len(chains) == 2:
        c = (chains[0] + chains[1]) * 0.5
    else:
        c = np.mean(np.vstack(chains), axis=0)
    diffs = [chains[i] - chains[j] for i in range(len(chains)) for j in range(i + 1, len(chains))]
    k = int(np.searchsorted(ts, ts[0] + pre_s))
    while k < n:
        a = int(np.searchsorted(ts, ts[k] - pre_s))
        if k - a < 5:
            k += 1
            continue
        ref_c = float(np.median(c[a:k]))
        if c[k] - ref_c < 1.5:
            k += 1
            continue
        m = k
        while m + 1 < n and c[m + 1] - ref_c >= 1.5:
            m += 1
        returned = m + 1 < n
        # widen by the ramp samples on either side (the driver smooths the edges over 0.1-0.4 s)
        lo_i, hi_i = k, m
        while lo_i - 1 > a and k - (lo_i - 1) <= PULSE_RAMP_N and c[lo_i - 1] - ref_c >= 0.5:
            lo_i -= 1
        while hi_i + 1 < n and (hi_i + 1) - m <= PULSE_RAMP_N and c[hi_i + 1] - ref_c >= 0.5:
            hi_i += 1
        dur = ts[m] - ts[k] + (ts[m + 1] - ts[m] if returned else 0.0)
        ok = (dur <= max_s
              and float(c[k:m + 1].max()) - ref_c >= min_db
              and all(float(np.abs(dd[lo_i:hi_i + 1] - np.median(dd[a:k])).max()) <= max_diff_db for dd in diffs)
              and all(float(np.ptp(x[a:k])) <= PULSE_PRE_RANGE for x in chains))
        if ok:
            mask[lo_i:hi_i + 1] = True
        k = m + 1
    return mask


def step_robust_var(x: np.ndarray, min_seg: int = 4) -> float:
    """Variance left after removing the best single level step anywhere in the window:
    min over split points k of the pooled within-segment variance (and the plain variance).
    A lone step (AP tx power, furniture, door) vanishes; fluctuating motion does not."""
    n = len(x)
    full = float(x.var())
    if n < 2 * min_seg:
        return full
    cs = np.concatenate(([0.0], np.cumsum(x)))
    cs2 = np.concatenate(([0.0], np.cumsum(x * x)))
    k = np.arange(min_seg, n - min_seg + 1)
    sse_l = cs2[k] - cs[k] ** 2 / k
    sse_r = (cs2[n] - cs2[k]) - (cs[n] - cs[k]) ** 2 / (n - k)
    return float(min(full, max(0.0, float((sse_l + sse_r).min()) / n)))


def robust_z(x: float, med: float, mad: float, floor: float) -> float:
    s = max(1.4826 * mad, floor)
    return (x - med) / s if s > 0 else 0.0


def finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)
