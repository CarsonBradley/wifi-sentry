"""The long-running engine: sources -> detector -> storage / SSE / alert sinks, plus the HTTP API."""
from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import net
from .alertcfg import CHANNELS, LABELS, AlertConfig, new_topic
from .arming import Arming
from .auth import Auth, load_or_create_token
from .config import LEVELS, WILDCARDS, bind_specs, resolve
from .detector import VERSION, Detector
from . import sinks
from .sinks import AlertRouter
from .sources import MacSource, start_esp32
from .storage import RawRecorder, Storage

log = logging.getLogger("sentry.app")

PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8"><title>wifi-sentry</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui;margin:2rem;max-width:40rem">
<h1>wifi-sentry engine</h1><p>The dashboard (web/) is not installed yet. The API is live:</p>
<ul><li><a href="/api/status">/api/status</a></li><li><a href="/api/events">/api/events</a></li>
<li><a href="/api/history?seconds=3600&amp;points=600">/api/history</a></li><li>/api/stream (SSE)</li></ul>
</body></html>"""


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def sse_format(event: str | None, data) -> bytes:
    out = ""
    if event:
        out += f"event: {event}\n"
    out += "data: " + json.dumps(data, separators=(",", ":")) + "\n\n"
    return out.encode()


def tighten_permissions(data_dir: Path, root: Path) -> None:
    """data/ holds when people are home, the token and alert secrets: owner-only (0700 / 0600), also for
    installs made before the engine set a umask. config.local.toml may hold alert passwords."""
    try:
        data_dir.chmod(0o700)
    except OSError:
        pass
    for base, dirs, files in os.walk(data_dir):
        for d in dirs:
            try:
                os.chmod(os.path.join(base, d), 0o700)
            except OSError:
                pass
        for f in files:
            p = os.path.join(base, f)
            try:
                if stat.S_IMODE(os.lstat(p).st_mode) & 0o077 and not os.path.islink(p):
                    os.chmod(p, 0o600)
            except OSError:
                pass
    local = root / "config.local.toml"
    try:
        if local.is_file() and stat.S_IMODE(local.stat().st_mode) & 0o077:
            local.chmod(0o600)
    except OSError:
        pass


class Engine:
    def __init__(self, cfg: dict, start_sources: bool = True):
        self.cfg = cfg
        self.start_sources = start_sources
        self.data_dir = resolve(cfg, cfg["paths"]["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(exist_ok=True)
        tighten_permissions(self.data_dir, Path(cfg.get("_root") or "."))
        self.walk_dir = self.data_dir / "walktests"
        self.storage = Storage(self.data_dir / "sentry.db")
        a = cfg["auth"]
        self.token_path = resolve({"_root": str(self.data_dir)}, a["token_file"])
        token, created = load_or_create_token(self.token_path)
        if created:
            log.info("generated a new access token in %s (./wifi-sentry url prints the links)", self.token_path)
        self.auth = Auth(token, cookie_name=f"wifi_sentry_{int(cfg['server']['port'])}",
                         loopback_exempt=bool(a["loopback_exempt"]), max_failures=int(a["max_failures"]),
                         failure_window_s=float(a["failure_window_s"]), cookie_days=float(a["cookie_days"]),
                         sessions_path=self.data_dir / "sessions.json")
        # bind spec -> address; replaced in tests. Specs: "loopback", "tailscale", "lan", literal addresses.
        self.resolve_spec = net.resolve_spec
        self.bind_state: dict[str, dict] = {}
        self.esp32_task: asyncio.Task | None = None
        self.esp32_bound: str | None = None
        self.detector = Detector(cfg, store=self.storage)
        # Alert channels: dashboard settings in data/alerts.json (0600), else the config file's [alerts.*].
        sinks.ALLOW_LOOPBACK_TARGETS = bool(cfg["alerts"].get("allow_loopback_targets", False))
        self.alert_cfg = AlertConfig(self.data_dir / "alerts.json", cfg)
        self.router = AlertRouter([], on_result=self._on_alert_result)
        self._apply_alert_cfg()
        self.arming: Arming | None = None
        self._arm_mode: str | None = None
        self._nw_since: float | None = None     # not watching since (offline / learning)
        self._nw_alerted = False
        self._last_tick: float | None = None
        self._down_gap: tuple[float, float] | None = None  # engine was down while armed (alerted at start)
        self._test_busy: set[str] = set()
        rec = cfg["record"]
        self.recorder = RawRecorder(self.data_dir / "raw", rec["keep_hours"], rec["max_bytes"]) if rec["enabled"] else None
        self.record_esp32 = bool(rec["include_esp32"])
        self.clients: set[asyncio.Queue] = set()
        self.mac: MacSource | None = None
        self.udp = None
        self.udp_proto = None
        self.tick_task: asyncio.Task | None = None
        self.rebind_task: asyncio.Task | None = None
        self.runner: web.AppRunner | None = None
        self.bound: list[str] = []
        self.walk_fh = None
        self.walk_path: Path | None = None
        self._last_save = 0.0
        self._last_prune = 0.0
        self._last_learning_push = 0.0
        self._last_heartbeat = 0.0
        self.gap: tuple[str, int] | None = None  # (reason, row id) of the coverage gap in progress
        self._restore()

    # ------------------------------------------------------------------ persistence
    def _restore(self) -> None:
        now = time.time()
        hb = self.storage.last_heartbeat()
        n = self.storage.close_dangling(hb)
        if n:
            log.info("closed %d event(s) left open by a previous run", n)
        if hb is not None:
            self.storage.close_open_gaps(hb)
            if now - hb > 3.0:
                self.storage.add_gap(hb, now, "engine_down")
                self._down_gap = (hb, now)
                log.info("engine was down for %.0f s (last heartbeat %s)", now - hb,
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hb)))
        sens = self.storage.kv_get("sensitivity")
        if sens in LEVELS:
            self.detector.sensitivity = sens
        self.arming = Arming(self.storage, float(self.cfg["arming"]["delay_s"]))
        self.detector.armed = self.arming.evaluate(now)
        self.router.import_state(self.storage.kv_get("alert_state"))
        if self.storage.kv_get("onboarding") is None:
            # First start of a version with onboarding: a fresh install gets the guided setup; an install
            # that has already recorded events is in use and is left alone (it only sees a small prompt
            # to set up phone alerts).
            existing = self.storage.last_event() is not None
            self.storage.kv_set("onboarding", {"status": "existing" if existing else "pending", "step": STEPS[0],
                                               "alerts_prompt_dismissed": False})
        bl = self.storage.kv_get("baselines")
        saved_at = self.storage.kv_updated("baselines")
        max_age = float(self.cfg["baseline"]["max_age_s"])
        if bl and saved_at and now - saved_at <= max_age:
            self.detector.import_baselines(bl)
            log.info("restored baselines for %s (saved %.0f s ago)", ", ".join(bl), now - saved_at)
        elif bl:
            log.info("stored baselines are older than %.0f s; relearning", max_age)
        last = self.storage.last_walktest()
        if last:
            self.detector.walk_result = last

    def save_state(self) -> None:
        self.storage.kv_set("baselines", self.detector.export_baselines())
        self.storage.kv_set("armed", self.detector.armed)  # effective state (the arm settings are in "arm_manual")
        self.storage.kv_set("sensitivity", self.detector.sensitivity)
        self.storage.kv_set("alert_state", self.router.export_state())

    # ------------------------------------------------------------------ ingest
    def on_sample(self, src: str, kind: str, payload: dict, t: float, raw: str | None) -> None:
        self.detector.ingest(src, kind, payload, t)
        line = None
        if self.recorder is not None and (kind == "mac" or self.record_esp32):
            line = raw if raw is not None else json.dumps(payload, separators=(",", ":"))
            self.recorder.write(line, t)
        if self.walk_fh is not None:
            if line is None:
                line = raw if raw is not None else json.dumps(payload, separators=(",", ":"))
            self.walk_fh.write(line + "\n")

    # ------------------------------------------------------------------ tick loop
    async def _tick_loop(self) -> None:
        while True:
            now = time.time()
            await asyncio.sleep(1.0 - (now % 1.0) + 0.002)
            try:
                self.tick(float(int(time.time())))
            except Exception:  # noqa: BLE001
                log.exception("tick failed")

    GAP_REASON = {"offline": "source_offline", "learning": "learning"}

    def _track_gap(self, state: str, t: float) -> None:
        reason = self.GAP_REASON.get(state)
        cur = self.gap[0] if self.gap else None
        if reason == cur:
            return
        if self.gap is not None:
            self.storage.close_gap(self.gap[1], t, min_len=3.0)  # drop start-up blips
            self.gap = None
        if reason is not None:
            self.gap = (reason, self.storage.open_gap(t, reason))

    def tick(self, t: float) -> None:
        last, self._last_tick = self._last_tick, t
        limit = float(self.cfg["alerts"].get("not_watching_s", 120) or 0)
        if last is not None and limit > 0 and t - last >= max(limit, 30) and self.detector.armed:
            # No tick for that long while the process lived: the Mac was asleep (or the process stopped).
            log.warning("no tick for %.0f s (Mac asleep?) while armed", t - last)
            self.router.handle_health("restart", {"since": last, "until": t}, self.status(), t)
        self._sync_arming(t)
        res = self.detector.tick(t)
        self.storage.add_history(t, res.score, res.threshold, res.state, res.sources)
        self._track_gap(res.state, t)
        self._watch_check(res.state, t)
        if res.state == "learning" and t - self._last_learning_push >= 3.0 \
                and not any(k == "status" for k, _ in res.emits):
            self._last_learning_push = t
            res.emits.append(("status", None))  # keeps the progress bar moving
        if t - self._last_heartbeat >= 5.0:
            self._last_heartbeat = t
            self.storage.kv_set("heartbeat", t)
        self.process_emits(res.emits, t)
        self.broadcast("tick", {"t": t, "state": res.state, "score": round(res.score, 3),
                                "threshold": res.threshold,
                                "sources": {k: (0.0 if v is None else round(v, 3)) for k, v in res.sources.items()}})
        now = time.time()
        if now - self._last_save >= float(self.cfg["baseline"]["save_every_s"]):
            self._last_save = now
            self.save_state()
            if self.recorder is not None:
                self.recorder.flush()
        if now - self._last_prune >= 3600:
            self._last_prune = now
            self.storage.prune(float(self.cfg["storage"]["history_keep_days"]))

    def process_emits(self, emits: list, t: float) -> None:
        status_needed = False
        for kind, obj in emits:
            if kind == "status":
                status_needed = True
            elif kind in ("event_open", "event_update", "event_close"):
                self.broadcast("motion", obj)
                if kind == "event_open":
                    log.info("event #%s opened (peak %.2f, sources %s, armed=%s)", obj["id"], obj["peak_score"],
                             obj["sources"], obj["armed"])
                    status = self.status()
                    if self.router.handle("open", obj, status, t):
                        self.broadcast("alert", obj)
                elif kind == "event_update":
                    if self.router.handle("update", obj, self.status(), t):
                        log.info("event #%s alerted again: the Mac's own sensor joined it", obj["id"])
                        self.broadcast("alert", obj)
                elif kind == "event_close":
                    log.info("event #%s closed after %.1f s (peak %.2f)", obj["id"], obj["duration_s"], obj["peak_score"])
                    self.router.handle("close", obj, self.status(), t)
            elif kind == "absorbed":
                log.info("baseline: steady disturbance on %s absorbed as the new normal", obj["source"])
            elif kind == "walktest":
                self._finish_walktest(obj)
        if status_needed:
            self.broadcast("status", self.status())

    # ------------------------------------------------------------------ "not watching" alerts
    def _not_watching_reason(self, state: str) -> str:
        if state == "learning":
            return "learning what the still room looks like (after a start or relearn)"
        for s in self.detector.status(time.time())["sources"]:
            if s.get("health"):
                return s["health"]
        return "no sensor is sending usable data (sensor offline)"

    def _watch_check(self, state: str, t: float) -> None:
        """Armed and not watching (sensor offline, blind, sampler crash-looping, learning) for
        [alerts] not_watching_s: one "can't sense" alert; one "watching again" when it recovers."""
        limit = float(self.cfg["alerts"].get("not_watching_s", 120) or 0)
        if state in ("offline", "learning"):
            if self._nw_since is None:
                self._nw_since = t
            if (limit > 0 and not self._nw_alerted and self.detector.armed and self.detector.walk is None
                    and t - self._nw_since >= limit):
                info = {"since": self._nw_since, "reason": self._not_watching_reason(state)}
                if self.router.handle_health("blind", info, self.status(), t):
                    self._nw_alerted = True
                    log.warning("armed but not watching since %s: %s (alert sent)",
                                time.strftime("%H:%M:%S", time.localtime(self._nw_since)), info["reason"])
        else:
            if self._nw_alerted:
                self.router.handle_health("watching", {"since": self._nw_since, "until": t}, self.status(), t)
            self._nw_since = None
            self._nw_alerted = False

    # ------------------------------------------------------------------ SSE
    def broadcast(self, event: str, data) -> None:
        msg = sse_format(event, data)
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow client: drop

    # ------------------------------------------------------------------ status/controls
    def status(self) -> dict:
        now = time.time()
        st = self.detector.status(now)
        st["arm"] = self.arming.public(now)
        st["alerts"] = self.alerts_summary()
        return st

    # ------------------------------------------------------------------ arming
    def _sync_arming(self, t: float) -> None:
        armed = self.arming.evaluate(t)
        mode = self.arming.mode(t)
        if armed != self.detector.armed:
            self.detector.set_armed(armed)  # emits a status push
            self.storage.kv_set("armed", armed)
            log.info("armed=%s (%s)", armed, mode)
        if mode != self._arm_mode:
            self._arm_mode = mode
            self.detector._emit("status")  # e.g. exit delay started, or the schedule took over

    def set_armed(self, armed: bool, delay_s: float | None = None) -> dict:
        """Manual arm/disarm. delay_s: exit delay before it is armed (None or 0 = at once, the 0.2 behaviour)."""
        now = time.time()
        self.arming.set_manual(armed, now, delay_s)
        log.info("arm request: armed=%s%s", armed, f" with a {delay_s:.0f} s exit delay" if armed and delay_s else "")
        self._sync_arming(now)
        return self._after_control()

    def set_arming(self, body: dict) -> dict:
        now = time.time()
        self.arming.set_settings(now, body.get("delay_s"), body.get("schedule"))
        log.info("arming settings: exit delay %d s, schedule %s (%d windows)", self.arming.delay_s,
                 "on" if self.arming.schedule_on else "off", len(self.arming.windows))
        self._sync_arming(now)
        self._after_control()
        return self.arming_view()

    def arming_view(self) -> dict:
        now = time.time()
        tz = self.arming.tz
        return {"settings": self.arming.settings(), "state": self.arming.public(now),
                "timezone": getattr(tz, "key", None) or str(tz)}

    # ------------------------------------------------------------------ alert channels
    def _apply_alert_cfg(self) -> None:
        s = self.alert_cfg.settings()
        self.router.configure(sinks=self.alert_cfg.build_sinks(), cooldown_s=s["cooldown_s"],
                              notify_on_close=s["notify_on_close"], base_url=s["base_url"],
                              health_cooldown_s=float(self.cfg["alerts"].get("not_watching_cooldown_s", 1800)))

    def _on_alert_result(self, rec: dict) -> None:
        self.storage.kv_set("alert_state", self.router.export_state())
        self.broadcast("alerts", {"result": rec, "summary": self.alerts_summary()})

    def alerts_summary(self) -> dict:
        """Enabled channels, and those whose latest real (non-test) delivery failed."""
        enabled = [s.id for s in self.router.sinks]
        failing = []
        for cid in enabled:
            r = self.router.results.get(cid)
            if r and not r["ok"] and r.get("kind") != "test":
                failing.append({"channel": cid, "label": LABELS.get(cid, cid), "t": r["t"], "error": r["error"]})
        return {"channels": enabled, "failing": failing}

    def alerts_view(self) -> dict:
        r = self.router
        return {
            "settings": self.alert_cfg.settings(),
            "channels": [dict(self.alert_cfg.public(k), last=r.results.get(k), last_ok=r.last_ok.get(k))
                         for k in CHANNELS],
            "cooldown": {"last_sent": r.last_sent if r.last_sent > 0 else None, "suppressed": r.suppressed},
            "summary": self.alerts_summary(),
            # Addresses the dashboard is reachable on from other devices, to offer as the link in alerts.
            "suggested_base_urls": [f"http://{b}/" for b in self.bound
                                    if not net.is_loopback_addr(b.rsplit(":", 1)[0].strip("[]"))],
        }

    def alerts_changed(self) -> dict:
        self._apply_alert_cfg()
        log.info("alert channels now: %s", ", ".join(s.id for s in self.router.sinks) or "none")
        view = self.alerts_view()
        self.broadcast("alerts", {"result": None, "summary": view["summary"]})
        self.broadcast("status", self.status())
        return view

    async def test_channel(self, kind: str) -> dict:
        sink = self.alert_cfg.sink(kind, require_enabled=False)
        if sink is None:
            ch, _ = self.alert_cfg.channel(kind)
            miss = self.alert_cfg.missing(kind, ch or {})
            raise ValueError(f"{LABELS[kind]} isn't set up yet: {', '.join(miss)} missing")
        if kind in self._test_busy:
            raise ValueError("a test for this channel is already being sent")
        self._test_busy.add(kind)
        try:
            log.info("sending a test alert via %s", kind)
            return await self.router.test(sink, self.status())
        finally:
            self._test_busy.discard(kind)

    # ------------------------------------------------------------------ onboarding
    def onboarding(self) -> dict:
        ob = dict(self.storage.kv_get("onboarding") or {"status": "existing", "step": STEPS[0]})
        show = ob.get("status") in ("pending", "active")
        return {"status": ob.get("status"), "step": ob.get("step") if ob.get("step") in STEPS else STEPS[0],
                "steps": list(STEPS), "show": show,
                "alerts_prompt": (not show and not self.router.sinks and not ob.get("alerts_prompt_dismissed"))}

    def set_onboarding(self, body: dict) -> dict:
        ob = dict(self.storage.kv_get("onboarding") or {"status": "existing", "step": STEPS[0]})
        for k, v in body.items():
            if k == "step":
                if v not in STEPS:
                    raise ValueError("step must be one of " + ", ".join(STEPS))
                ob["step"] = v
            elif k == "status":
                if v not in ("active", "done", "skipped"):
                    raise ValueError("status must be active, done or skipped")
                ob["status"] = v
            elif k == "alerts_prompt_dismissed":
                if not isinstance(v, bool):
                    raise ValueError("alerts_prompt_dismissed must be a boolean")
                ob[k] = v
            else:
                raise ValueError(f"unknown onboarding field {k!r}")
        self.storage.kv_set("onboarding", ob)
        return self.onboarding()

    def set_sensitivity(self, level: str) -> dict:
        self.detector.set_sensitivity(level)
        self.storage.kv_set("sensitivity", level)
        log.info("sensitivity=%s", level)
        return self._after_control()

    def relearn(self) -> dict:
        self.detector.relearn(time.time())
        self.storage.kv_set("baselines", {})
        log.info("relearn requested: baselines cleared")
        self._track_gap(self.detector.state, time.time())
        return self._after_control()

    def _after_control(self) -> dict:
        """Process detector emits and always push the status to every open stream (other tabs)."""
        now = time.time()
        self.process_emits([e for e in self.detector.drain_emits() if e[0] != "status"], now)
        st = self.status()
        self.broadcast("status", st)
        return st

    def walktest(self, action: str) -> dict:
        now = time.time()
        if action == "start":
            if self.detector.walk is None:
                self.walk_dir.mkdir(parents=True, exist_ok=True)
                self.walk_path = self.walk_dir / time.strftime("walktest-%Y%m%d-%H%M%S.jsonl", time.localtime(now))
                self.walk_fh = open(self.walk_path, "w", buffering=1 << 16)
                log.info("walk test started -> %s", self.walk_path)
            self.detector.walktest_start(now)
        elif action == "stop":
            self.detector.walktest_stop(now)
        else:
            raise ValueError("action must be start|stop")
        self.process_emits(self.detector.drain_emits(), now)
        return self.detector.walktest_state()

    def _finish_walktest(self, state: dict) -> None:
        if self.walk_fh is not None:
            self.walk_fh.close()
            self.walk_fh = None
        res = state.get("result")
        if res is None:
            return
        raw = str(self.walk_path) if self.walk_path else ""
        res = dict(res, raw_path=raw)
        self.detector.walk_result = res
        if self.walk_path:
            with open(self.walk_path.with_suffix(".result.json"), "w") as f:
                json.dump(res, f, indent=1)
        self.storage.add_walktest(res["started"], res, raw)
        log.info("walk test done: peak %.2f median %.2f recommended %s", res["peak_score"], res["median_score"],
                 res["recommended"])
        self.broadcast("walktest", self.detector.walktest_state())

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.start_sources:
            root = self.cfg["_root"]
            if self.cfg["mac"]["enabled"]:
                cmd = list(self.cfg["mac"]["command"])
                cmd[0] = str(resolve(self.cfg, cmd[0]))
                self.mac = MacSource(cmd, root, self.on_sample, float(self.cfg["mac"]["restart_min_s"]),
                                     float(self.cfg["mac"]["restart_max_s"]))
                self.mac.start()
            if self.cfg["esp32"]["enabled"]:
                if not await self._start_esp32(first=True):
                    self.esp32_task = asyncio.create_task(self._esp32_retry(), name="esp32-bind")
        self.tick_task = asyncio.create_task(self._tick_loop(), name="tick")
        limit = float(self.cfg["alerts"].get("not_watching_s", 120) or 0)
        if self._down_gap and limit > 0 and self._down_gap[1] - self._down_gap[0] >= limit and self.detector.armed:
            since, until = self._down_gap
            self.router.handle_health("restart", {"since": since, "until": until}, self.status(), until)

    async def _start_esp32(self, first: bool = False) -> bool:
        """Bind the ESP32 UDP listener per [esp32] bind ("lan" = this Mac's Wi-Fi address by default).
        Without [esp32] key the port is unauthenticated (see SECURITY.md); datagrams are validated before they
        reach the detector, and with a key unsigned or wrongly signed ones are dropped."""
        spec = str(self.cfg["esp32"]["bind"])
        port = int(self.cfg["esp32"]["port"])
        addr = "0.0.0.0" if spec == "0.0.0.0" else await asyncio.to_thread(self.resolve_spec, spec, self.cfg["_root"])
        if addr is None:
            if first:
                log.warning("ESP32 UDP listener: no address for %r yet (Wi-Fi not connected?); retrying every %.0f s",
                            spec, self.REBIND_EVERY_S)
            return False
        if addr == "0.0.0.0":
            log.warning("ESP32 UDP listener on ALL interfaces (esp32.bind = \"0.0.0.0\")%s",
                        "" if self.cfg["esp32"].get("key") else "; it is unauthenticated (no [esp32] key)")
        try:
            self.udp, self.udp_proto = await start_esp32(addr, port, self.on_sample, key=self.cfg["esp32"].get("key", ""),
                                                         on_expire=self.detector.forget_source)
        except OSError as e:
            if first:
                log.error("ESP32 UDP listener could not bind %s:%s: %s; retrying every %.0f s", addr, port, e,
                          self.REBIND_EVERY_S)
            return False
        self.esp32_bound = f"{addr}:{port}"
        return True

    async def _esp32_retry(self) -> None:
        while not await self._start_esp32():
            await asyncio.sleep(self.REBIND_EVERY_S)

    async def serve_http(self, hosts: list[str] | None = None, port: int | None = None) -> list[str]:
        """Listen on 127.0.0.1 plus whatever [server] expose asks for ("tailscale", "lan", addresses).
        `hosts` (tests, --hosts) replaces the whole list. A spec without an address yet (Tailscale not
        up at login, Wi-Fi not connected) or an address that can't be bound is retried every 30 s.
        Wildcard addresses are refused. Anything beyond loopback requires the access token (auth.py)."""
        app = create_app(self)
        self.runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self.runner.setup()
        specs = list(hosts) if hosts is not None else bind_specs(self.cfg)
        port = int(port if port is not None else self.cfg["server"]["port"])
        missing = []
        for spec in specs:
            self.bind_state[spec] = {"spec": spec, "address": None, "bound": False, "error": None}
            if not await self._bind_spec(spec, port, first=True):
                if self.bind_state[spec]["error"] != "wildcard refused":
                    missing.append(spec)
        if missing:
            self.rebind_task = asyncio.create_task(self._rebind_loop(missing, port), name="rebind")
        return self.bound

    async def _bind_spec(self, spec: str, port: int, first: bool = False) -> bool:
        st = self.bind_state.setdefault(spec, {"spec": spec, "address": None, "bound": False, "error": None})
        if spec in WILDCARDS:
            log.error("refusing to bind wildcard address %r (use \"lan\" or an explicit address)", spec)
            st["error"] = "wildcard refused"
            return False
        addr = await asyncio.to_thread(self.resolve_spec, spec, self.cfg["_root"])
        if addr in WILDCARDS:
            log.error("refusing to bind wildcard address %r for %r", addr, spec)
            st["error"] = "wildcard refused"
            return False
        if addr is None:
            st["error"] = "no address yet"
            if first:
                why = {"tailscale": "Tailscale not installed or not connected",
                       "lan": "Wi-Fi not connected"}.get(spec, "address not available")
                log.warning("not listening on %r yet (%s); retrying every %.0f s", spec, why, self.REBIND_EVERY_S)
            return False
        st["address"] = addr
        ok = await self._bind(addr, port, first=first)
        st["bound"] = ok
        st["error"] = None if ok else st.get("error") or "bind failed"
        return ok

    async def _bind(self, h: str, port: int, first: bool = False) -> bool:
        site = web.TCPSite(self.runner, h, port, reuse_address=True)
        try:
            await site.start()
        except OSError as e:
            for st in self.bind_state.values():
                if st["address"] == h:
                    st["error"] = str(e)
            if first:
                if e.errno == errno.EADDRINUSE and port:
                    log.error("port %d on %s is already in use (another program, or another wifi-sentry?). "
                              "Find it with: lsof -nP -iTCP:%d -sTCP:LISTEN ; or set [server] port in "
                              "config.local.toml. Retrying every %.0f s", port, h, port, self.REBIND_EVERY_S)
                else:
                    log.error("could not bind %s:%d (%s); will keep retrying every %.0f s", h, port, e,
                              self.REBIND_EVERY_S)
            return False
        self.bound.append(f"{h}:{port}")
        sinks.SELF_BOUND.add((ipaddress.ip_address(h).compressed if _is_ip(h) else h, port))
        log.info("HTTP listening on http://%s:%d/%s", h, port,
                 "" if net.is_loopback_addr(h) else " (access token required)")
        return True

    REBIND_EVERY_S = 30.0

    async def _rebind_loop(self, specs: list[str], port: int) -> None:
        while specs:
            await asyncio.sleep(self.REBIND_EVERY_S)
            specs = [sp for sp in specs if not await self._bind_spec(sp, port)]

    async def stop(self) -> None:
        now = time.time()
        if self.rebind_task:
            self.rebind_task.cancel()
        if self.esp32_task:
            self.esp32_task.cancel()
        if self.tick_task:
            self.tick_task.cancel()
            try:
                await self.tick_task
            except asyncio.CancelledError:
                pass
        if self.detector.walk is not None:
            self.detector.walktest_stop(now)
        if self.detector.event is not None:
            self.detector._close(now, "engine stopped")
        self.process_emits(self.detector.drain_emits(), now)
        if self.gap is not None:
            self.storage.close_gap(self.gap[1], now, min_len=3.0)
            self.gap = None
        self.storage.kv_set("heartbeat", now)
        if self.mac:
            await self.mac.stop()
        if self.udp:
            self.udp.close()
        for q in list(self.clients):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self.runner:
            await self.runner.cleanup()
        await self.router.drain(timeout=5)  # an alert still retrying gets a few seconds, not the full backoff
        self.save_state()
        if self.recorder:
            self.recorder.close()
        if self.walk_fh:
            self.walk_fh.close()
        self.storage.close()


# ====================================================================== HTTP API
OPEN_API = ("/api/auth", "/api/login", "/api/logout")
STEPS = ("intro", "sensor", "learning", "walk", "alerts", "arm")  # first-run setup, in order
ENGINE_KEY: web.AppKey["Engine"] = web.AppKey("engine")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",            # nobody can frame the arm switch
    "Referrer-Policy": "no-referrer",     # a ?token= URL never leaks through a Referer
}


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin or origin == "null":
        return origin is None
    try:
        o = urlsplit(origin)
    except ValueError:
        return False
    return o.netloc.lower() == (request.host or "").lower()


def _unauthorized(auth: Auth, request: web.Request, msg: str = "access token required") -> web.Response:
    resp = web.json_response({"error": msg, "auth": "required"}, status=401,
                             headers={"WWW-Authenticate": 'Bearer realm="wifi-sentry"'})
    if request.cookies.get(auth.cookie_name):
        auth.clear_cookie(resp)  # stale cookie (token rotated): drop it
    return resp


def _too_many(wait: float) -> web.Response:
    return web.json_response({"error": f"too many wrong access tokens; try again in {wait:.0f} s",
                              "retry_after": round(wait)}, status=429, headers={"Retry-After": str(round(wait))})


def _check_access(engine: "Engine", request: web.Request) -> web.StreamResponse | None:
    """None if the request may proceed, else the response to send instead."""
    auth = engine.auth
    path = request.path
    if path.startswith("/api/"):
        if path in OPEN_API or auth.is_local(request):
            return None
        bearer = auth.bearer(request)
        if bearer is not None:
            if auth.token_ok(bearer):  # a correct token always works, even while its address is locked out
                return None
            ip = request.remote or "?"
            wait = auth.blocked_for(ip)
            if wait:
                return _too_many(wait)
            auth.record_failure(ip, "bearer")
            return _unauthorized(auth, request, "wrong access token")
        if auth.cookie_ok(request):
            return None
        return _unauthorized(auth, request)
    if request.method == "GET" and "token" in request.query:
        # Dashboard link with ?token=...: set the session cookie, then go to the dashboard without the
        # token so it stays out of the address bar, history and bookmarks. Always to "/" (a fixed,
        # same-origin target): echoing the request path back made "//evil.example/?token=x" an open redirect.
        resp = web.Response(status=303, headers={"Location": "/", "Cache-Control": "no-store"})
        ip = request.remote or "?"
        if auth.token_ok(request.query.get("token")):
            auth.set_cookie(resp)
        elif auth.blocked_for(ip):
            resp.headers["Location"] = "/#too-many-attempts"
        else:
            auth.record_failure(ip, "link")
            resp.headers["Location"] = "/#token-rejected"
        return resp
    return None


@web.middleware
async def guard(request: web.Request, handler):
    engine = request.app[ENGINE_KEY]
    # No CORS headers are ever sent, so browsers only allow same-origin reads. For state-changing
    # requests also reject a cross-origin Origin header (stops another site from disarming).
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        resp = web.json_response({"error": "cross-origin request refused"}, status=403)
    else:
        resp = _check_access(engine, request)
    if resp is None:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            e.headers.update(SECURITY_HEADERS)
            if not request.path.startswith("/api/"):
                e.headers["Cache-Control"] = "no-cache"
            raise
        except ValueError as e:
            resp = web.json_response({"error": str(e)}, status=400)
    if not resp.prepared:
        resp.headers.update(SECURITY_HEADERS)
        if not request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"
    return resp


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("body must be JSON")
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def create_app(engine: Engine) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app[ENGINE_KEY] = engine
    web_dir = resolve(engine.cfg, engine.cfg["server"]["web_dir"]).resolve()
    r = app.router
    auth = engine.auth

    async def auth_state(request):
        local = auth.is_local(request)
        return web.json_response({"local": local, "required": not local,
                                  "authenticated": local or auth.cookie_ok(request)
                                  or auth.token_ok(auth.bearer(request))})

    async def login(request):
        ip = request.remote or "?"
        body = await _json_body(request)
        tok = body.get("token")
        if isinstance(tok, str) and auth.token_ok(tok):
            resp = web.json_response({"ok": True})
            auth.set_cookie(resp)
            return resp
        wait = auth.blocked_for(ip)
        if wait:
            return _too_many(wait)
        auth.record_failure(ip, "login")
        return web.json_response({"error": "wrong access token"}, status=401)

    async def logout(request):
        resp = web.json_response({"ok": True})
        auth.logout(request, resp)
        return resp

    async def status(request):
        return web.json_response(engine.status())

    async def events(request):
        try:
            limit = int(request.query.get("limit", 50))
            before = request.query.get("before")
            before = int(before) if before not in (None, "", "null") else None
        except ValueError:
            raise ValueError("limit/before must be integers")
        return web.json_response({"events": engine.storage.list_events(limit, before)})

    async def label(request):
        try:
            eid = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        if "label" not in body or body["label"] not in ("real", "false", None):
            raise ValueError('label must be "real", "false" or null')
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string")
        if note is not None:
            note = note[:2000]
        if engine.storage.get_event(eid) is None:
            return web.json_response({"error": "no such event"}, status=404)
        ev = engine.storage.label_event(eid, body["label"], note)
        engine.detector.label_event(ev)
        return web.json_response(ev)

    async def history(request):
        try:
            seconds = float(request.query.get("seconds", 3600))
            points = int(request.query.get("points", 600))
        except ValueError:
            raise ValueError("seconds/points must be numbers")
        return web.json_response(engine.storage.history(seconds, points))

    async def gaps(request):
        """Coverage gaps (engine down, no sensor online, learning) overlapping [since, until]."""
        now = time.time()
        try:
            since = float(request.query.get("since", now - 86400))
            until = float(request.query.get("until", now))
        except ValueError:
            raise ValueError("since/until must be numbers")
        since = max(since, now - 31 * 86400)
        return web.json_response({"gaps": engine.storage.gaps(since, min(until, now))})

    async def arm(request):
        """{"armed": bool, "delay_s": optional exit delay (0..3600; default 0 = at once)}"""
        body = await _json_body(request)
        if not isinstance(body.get("armed"), bool):
            raise ValueError("armed must be a boolean")
        delay = body.get("delay_s")
        if delay is not None and (isinstance(delay, bool) or not isinstance(delay, (int, float))):
            raise ValueError("delay_s must be a number of seconds")
        return web.json_response(engine.set_armed(body["armed"], delay))

    async def arming_get(request):
        return web.json_response(engine.arming_view())

    async def arming_post(request):
        body = await _json_body(request)
        unknown = set(body) - {"delay_s", "schedule"}
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        return web.json_response(engine.set_arming(body))

    def _kind(request) -> str:
        kind = request.match_info["kind"]
        if kind not in CHANNELS:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such channel"}), content_type="application/json")
        return kind

    async def alerts_get(request):
        return web.json_response(engine.alerts_view())

    async def alerts_settings(request):
        engine.alert_cfg.update_settings(await _json_body(request))
        return web.json_response(engine.alerts_changed())

    async def channel_post(request):
        kind = _kind(request)
        engine.alert_cfg.update_channel(kind, await _json_body(request))
        return web.json_response(engine.alerts_changed())

    async def channel_delete(request):
        engine.alert_cfg.reset_channel(_kind(request))
        return web.json_response(engine.alerts_changed())

    async def channel_test(request):
        res = await engine.test_channel(_kind(request))
        return web.json_response(res)

    async def ntfy_topic(request):
        return web.json_response({"topic": new_topic()})

    async def onboarding_get(request):
        return web.json_response(engine.onboarding())

    async def onboarding_post(request):
        return web.json_response(engine.set_onboarding(await _json_body(request)))

    async def sensitivity(request):
        body = await _json_body(request)
        if body.get("level") not in LEVELS:
            raise ValueError("level must be low|medium|high")
        return web.json_response(engine.set_sensitivity(body["level"]))

    async def relearn(request):
        return web.json_response(engine.relearn())

    async def walktest_post(request):
        body = await _json_body(request)
        return web.json_response(engine.walktest(str(body.get("action"))))

    async def walktest_get(request):
        return web.json_response(engine.detector.walktest_state())

    async def debug(request):
        mac = engine.mac
        return web.json_response({
            "detector": engine.detector.debug(),
            "alerts": engine.router.log,
            "sinks": [s.id for s in engine.router.sinks],
            "mac_process": None if mac is None else {
                "pid": mac.proc.pid if mac.proc and mac.proc.returncode is None else None,
                "restarts": mac.restarts, "last_error": mac.last_error},
            "esp32_listener": None if engine.udp_proto is None else engine.udp_proto.counters(),
            "bound": engine.bound,
            "bind": list(engine.bind_state.values()),
            "esp32_bound": engine.esp32_bound,
            "auth": {"loopback_exempt": auth.loopback_exempt, "failed_attempts": auth.total_failures,
                     "token_file": str(engine.token_path)},
            "config_notes": engine.cfg.get("_notes", []),
            "config_files": engine.cfg.get("_files", []),
            "version": VERSION,
        })

    async def stream(request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        engine.clients.add(q)
        try:
            await resp.write(b"retry: 3000\n\n" + sse_format("status", engine.status()))
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 15.0)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                if msg is None:
                    break
                await resp.write(msg)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            engine.clients.discard(q)
        return resp

    async def static(request):
        rel = request.match_info.get("path", "") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        if not web_dir.is_dir():
            if rel == "index.html":
                return web.Response(text=PLACEHOLDER, content_type="text/html")
            raise web.HTTPNotFound()
        # Only the dashboard's own assets: files directly in web/ with a known type. Nothing in
        # sub-directories (web/dev/ holds test scripts and screenshots) and no dot-files.
        if "/" in rel or "\\" in rel or rel.startswith(".") or Path(rel).suffix.lower() not in CONTENT_TYPES:
            raise web.HTTPNotFound()
        p = (web_dir / rel).resolve()
        if p.parent != web_dir:
            raise web.HTTPNotFound()
        if not p.is_file():
            if rel == "index.html":
                return web.Response(text=PLACEHOLDER, content_type="text/html")
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "no-cache"}
        ctype = CONTENT_TYPES.get(p.suffix.lower())
        if ctype:
            headers["Content-Type"] = ctype
        return web.FileResponse(p, headers=headers)

    r.add_get("/api/auth", auth_state)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_get("/api/status", status)
    r.add_get("/api/events", events)
    r.add_post("/api/events/{id}/label", label)
    r.add_get("/api/history", history)
    r.add_get("/api/gaps", gaps)
    r.add_get("/api/stream", stream)
    r.add_post("/api/arm", arm)
    r.add_get("/api/arming", arming_get)
    r.add_post("/api/arming", arming_post)
    r.add_get("/api/alerts", alerts_get)
    r.add_post("/api/alerts/settings", alerts_settings)
    r.add_post("/api/alerts/ntfy-topic", ntfy_topic)
    r.add_post("/api/alerts/channels/{kind}", channel_post)
    r.add_delete("/api/alerts/channels/{kind}", channel_delete)
    r.add_post("/api/alerts/channels/{kind}/test", channel_test)
    r.add_get("/api/onboarding", onboarding_get)
    r.add_post("/api/onboarding", onboarding_post)
    r.add_post("/api/sensitivity", sensitivity)
    r.add_post("/api/relearn", relearn)
    r.add_post("/api/walktest", walktest_post)
    r.add_get("/api/walktest", walktest_get)
    r.add_get("/api/debug", debug)

    async def api_404(request):
        return web.json_response({"error": "not found"}, status=404)

    r.add_route("*", "/api/{tail:.*}", api_404)
    r.add_get("/", static)
    r.add_get("/{path:.*}", static)
    return app
