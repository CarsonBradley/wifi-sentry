#!/usr/bin/env python3
"""Mock wifi-sentry engine for developing the web dashboard.

Stdlib only, Python 3.9+. Serves ../ (the web/ directory) at / and implements
the full HTTP + SSE API contract with simulated data.

    python3 web/dev/mock_server.py                      # demo: motion every 60 s
    python3 web/dev/mock_server.py --scenario idle
    python3 web/dev/mock_server.py --scenario learning
    python3 web/dev/mock_server.py --scenario motion --motion-every 30
    python3 web/dev/mock_server.py --scenario offline   # Mac sensor drops out
    python3 web/dev/mock_server.py --scenario esp32     # adds a second source
    python3 web/dev/mock_server.py --scenario flaky     # server dies and returns
    python3 web/dev/mock_server.py --die-every 40 --down-for 15 --esp32

Binds 127.0.0.1 only. Default port 8451.

Test hooks (not part of the real contract):
    POST /_mock/set {"burst": seconds}          start a motion burst now
    POST /_mock/set {"offline": true|false}     take the Mac source offline/online
    POST /_mock/set {"learning": seconds}       enter learning for N seconds
    POST /_mock/set {"die": seconds}            drop every connection, refuse for N s
    POST /_mock/set {"esp32": true|false}       add/remove the esp32 source
    POST /_mock/set {"freeze": seconds}         keep connections open but stop sending ticks
"""

import argparse
import json
import math
import os
import queue
import random
import socket
import statistics
import sys
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
THRESHOLDS = {"low": 4.0, "medium": 2.5, "high": 1.6}
VERSION = "0.1.0-mock"
CLOSE_AFTER_QUIET_S = 3


def now():
    return time.time()


class Sim:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.rng = random.Random(args.seed)
        self.started = now()
        self.sensitivity = "medium"
        self.armed = True
        self.sources = {
            "mac": {"id": "mac", "kind": "mac", "online": True, "rate_hz": 10.0,
                    "last_seen": now(), "score": 0.0, "rssi": -42},
        }
        if args.esp32:
            self._add_esp32()
        self.state = "clear"
        self.since = now() - 2 * 3600 - 840
        self.learning_until = 0.0
        self.learning_total = 1.0
        self.score = 0.0
        self.samples = deque(maxlen=26 * 3600 + 600)  # (t, score, thr, {src: score})
        self.events = []  # oldest first
        self.next_id = 1
        self.current = None
        self.quiet_ticks = 0
        self.bursts = []  # (start, end, amp, sources)
        self.next_burst = now() + (args.motion_every or 0) if args.motion_every else None
        self.offline_until = 0.0
        self.mac_forced_offline = False
        self.walk = {"running": False, "started": None, "result": None, "scores": [], "amp": 0.0}
        self.subs = []
        self.server_epoch = 0
        self.frozen_until = 0.0
        self._seed_history()
        if self.events:
            self.since = self.events[-1]["end"]
        if args.scenario == "learning":
            self.start_learning(args.learn_seconds)
        if args.scenario == "offline":
            self.mac_forced_offline = True
            self.sources["mac"]["online"] = False
            self.sources["mac"]["last_seen"] = now() - 95
        self._update_state(now(), initial=True)

    def _add_esp32(self):
        self.sources["esp32-hall"] = {"id": "esp32-hall", "kind": "esp32", "online": True,
                                      "rate_hz": 100.0, "last_seen": now(), "score": 0.0,
                                      "rssi": -61}

    # ---------- history seeding ----------
    def _seed_history(self):
        t_end = now()
        t0 = t_end - 26 * 3600
        rng = random.Random(7)
        # scripted past events: (seconds_ago, duration, amp, armed, label, note)
        script = [
            (25.2 * 3600, 35, 5.5, False, "real", "Me leaving for work"),
            (22.0 * 3600, 12, 3.0, True, "false", "Furnace kicked on?"),
            (19.5 * 3600, 70, 6.5, True, "real", ""),
            (15.1 * 3600, 8, 2.8, True, None, ""),
            (9.6 * 3600, 22, 4.2, False, None, ""),
            (5.3 * 3600, 48, 7.0, True, None, ""),
            (3.2 * 3600, 6, 2.7, True, None, ""),
            (2.4 * 3600, 18, 3.9, True, None, ""),
        ]
        if self.args.many_events:
            for k in range(self.args.many_events):
                ago = 3600 + k * (24.0 * 3600 / self.args.many_events) + rng.uniform(0, 300)
                script.append((ago, rng.randint(4, 30), rng.uniform(2.8, 7.0), rng.random() < 0.7, None, ""))
        bursts = [(t_end - ago, t_end - ago + dur, amp, armed, lab, note)
                  for ago, dur, amp, armed, lab, note in script]
        thr = THRESHOLDS["medium"]
        has_esp = "esp32-hall" in self.sources
        t = t0
        cur = None
        quiet = 0
        while t < t_end - 1:
            base = abs(rng.gauss(0.42, 0.18)) + 0.08 * math.sin(t / 900.0)
            add = 0.0
            active = None
            for b in bursts:
                if b[0] <= t <= b[1]:
                    ph = (t - b[0]) / max(1, b[1] - b[0])
                    add = b[2] * math.sin(math.pi * ph) ** 0.6 * (0.75 + 0.25 * rng.random())
                    active = b
            s = round(max(0.0, base + add), 3)
            per = {"mac": s}
            if has_esp:
                per["esp32-hall"] = round(max(0.0, s * 0.8 + rng.gauss(0, 0.1)), 3)
            self.samples.append((t, s, thr, per))
            if s > thr and active:
                if cur is None:
                    cur = {"id": self.next_id, "start": t, "end": None, "duration_s": 0.0,
                           "peak_score": s, "sources": ["mac"], "armed": active[3],
                           "label": active[4], "note": active[5]}
                    self.next_id += 1
                cur["peak_score"] = max(cur["peak_score"], s)
                quiet = 0
            elif cur is not None:
                quiet += 1
                if quiet >= CLOSE_AFTER_QUIET_S:
                    cur["end"] = t
                    cur["duration_s"] = round(cur["end"] - cur["start"], 1)
                    self.events.append(cur)
                    cur = None
                    quiet = 0
            t += 1.0

    # ---------- pub/sub ----------
    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, name, data):
        msg = "event: %s\ndata: %s\n\n" % (name, json.dumps(data))
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    # ---------- state ----------
    def threshold(self):
        return THRESHOLDS[self.sensitivity]

    def status(self):
        with self.lock:
            prog = 1.0
            if self.state == "learning":
                prog = 1.0 - max(0.0, self.learning_until - now()) / self.learning_total
            return {
                "state": self.state,
                "armed": self.armed,
                "since": self.since,
                "score": round(self.score, 3),
                "threshold": self.threshold(),
                "sensitivity": self.sensitivity,
                "learning_progress": round(min(1.0, max(0.0, prog)), 3),
                "sources": [dict(s, score=round(s["score"], 3)) for s in self.sources.values()],
                "uptime_s": round(now() - self.started, 1),
                "version": VERSION,
            }

    def start_learning(self, seconds):
        with self.lock:
            self.learning_total = float(seconds)
            self.learning_until = now() + seconds
            if self.current:
                self._close_event(now())
            self._update_state(now())

    def _update_state(self, t, initial=False):
        any_online = any(s["online"] for s in self.sources.values())
        if not any_online:
            st = "offline"
        elif self.learning_until > t:
            st = "learning"
        elif self.current is not None:
            st = "motion"
        else:
            st = "clear"
        if st != self.state:
            self.state = st
            self.since = t
            if not initial:
                self.publish("status", self.status())

    def _close_event(self, t):
        ev = self.current
        ev["end"] = t
        ev["duration_s"] = round(t - ev["start"], 1)
        self.current = None
        self.quiet_ticks = 0
        self.publish("motion", ev)

    def tick(self):
        t = now()
        if t < self.frozen_until:
            return
        with self.lock:
            a = self.args
            # scheduled bursts
            if self.next_burst and t >= self.next_burst and self.state not in ("learning", "offline"):
                self.add_burst(self.rng.uniform(8, 40))
                self.next_burst = t + a.motion_every
            # offline scenario: the Mac source starts offline and flips every offline_cycle s
            mac = self.sources["mac"]
            mac_online = not self.mac_forced_offline
            if a.scenario == "offline" and a.offline_cycle:
                mac_online = int((t - self.started) // a.offline_cycle) % 2 == 1
            sources_changed = False
            if mac_online != mac["online"]:
                mac["online"] = mac_online
                sources_changed = True
            # score
            base = abs(self.rng.gauss(0.42, 0.18))
            add = 0.0
            srcs = set()
            for (b0, b1, amp, bsrc) in self.bursts:
                if b0 <= t <= b1:
                    ph = (t - b0) / max(1.0, b1 - b0)
                    add = max(add, amp * math.sin(math.pi * ph) ** 0.5 * (0.7 + 0.3 * self.rng.random()))
                    srcs.update(bsrc)
            self.bursts = [b for b in self.bursts if b[1] > t]
            if self.walk["running"]:
                add = max(add, self.walk["amp"] * (0.4 + 0.6 * abs(math.sin(t / 3.1))) * (0.7 + 0.3 * self.rng.random()))
                srcs.add("mac")
            learning = self.learning_until > t
            if learning:
                add *= 0.0
            score = base + add
            per = {}
            for sid, s in self.sources.items():
                if not s["online"]:
                    s["score"] = 0.0
                    continue
                s["last_seen"] = t - self.rng.uniform(0, 0.2)
                s["rssi"] = int(round((-42 if s["kind"] == "mac" else -61) + self.rng.gauss(0, 1.2) - add * 0.6))
                f = 1.0 if sid == "mac" else (0.85 if sid in srcs or not srcs else 0.3)
                s["score"] = max(0.0, score * f + self.rng.gauss(0, 0.05))
                per[sid] = round(s["score"], 3)
            online = [s for s in self.sources.values() if s["online"]]
            self.score = max([s["score"] for s in online], default=0.0)
            thr = self.threshold()
            if self.walk["running"]:
                self.walk["scores"].append(self.score)
                if t - self.walk["started"] > 120:
                    self.stop_walk()
            # detection
            if online and not learning and not self.walk["running"]:
                if self.score > thr:
                    if self.current is None:
                        self.current = {"id": self.next_id, "start": t, "end": None,
                                        "duration_s": 0.0, "peak_score": round(self.score, 3),
                                        "sources": sorted(sid for sid in per if per[sid] > thr) or ["mac"],
                                        "armed": self.armed, "label": None, "note": ""}
                        self.next_id += 1
                        self.events.append(self.current)
                        self.publish("motion", self.current)
                    ev = self.current
                    ev["peak_score"] = round(max(ev["peak_score"], self.score), 3)
                    for sid in per:
                        if per[sid] > thr and sid not in ev["sources"]:
                            ev["sources"].append(sid)
                    ev["duration_s"] = round(t - ev["start"], 1)
                    self.quiet_ticks = 0
                    if int(ev["duration_s"]) % 3 == 0:
                        self.publish("motion", ev)
                elif self.current is not None:
                    self.quiet_ticks += 1
                    if self.quiet_ticks >= CLOSE_AFTER_QUIET_S:
                        self._close_event(t)
            elif self.current is not None and (learning or not online):
                self._close_event(t)
            self.samples.append((t, round(self.score, 3) if online else None, thr, per))
            prev = self.state
            self._update_state(t)
            if sources_changed and prev == self.state:
                self.publish("status", self.status())
            self.publish("tick", {"t": t, "state": self.state, "score": round(self.score, 3),
                                  "threshold": thr, "sources": per})
            if self.state == "learning" and int(t) % 3 == 0:
                self.publish("status", self.status())

    def add_burst(self, seconds, amp=None, sources=None):
        with self.lock:
            t = now()
            amp = amp if amp is not None else self.rng.uniform(3.0, 7.5)
            src = sources or (["mac", "esp32-hall"] if "esp32-hall" in self.sources and self.rng.random() < 0.6 else ["mac"])
            self.bursts.append((t, t + seconds, amp, src))

    # ---------- walk test ----------
    def walk_view(self):
        w = self.walk
        return {"running": w["running"], "started": w["started"], "result": w["result"]}

    def start_walk(self):
        with self.lock:
            self.walk = {"running": True, "started": now(), "result": None, "scores": [],
                         "amp": self.rng.choice([1.3, 2.2, 3.4, 5.0, 6.5]) if self.args.walk_amp is None else self.args.walk_amp}
            if self.current:
                self._close_event(now())
            return self.walk_view()

    def stop_walk(self):
        with self.lock:
            w = self.walk
            if not w["running"]:
                return self.walk_view()
            scores = w["scores"] or [0.0]
            idle = [s[1] for s in list(self.samples)[-600:-len(scores) - 5] if s[1] is not None] or [0.4]
            detected = {lvl: sum(1 for s in scores if s > thr) >= 2 for lvl, thr in THRESHOLDS.items()}
            rec = "none"
            for lvl in ("low", "medium", "high"):
                if detected[lvl]:
                    rec = lvl
                    break
            if rec == "none":
                msg = ("Your walk barely stood out from the quiet baseline, even on High. "
                       "Try walking closer to the line between this computer and the router, "
                       "or add an ESP32 sensor in the rooms you care about.")
            elif rec == "low":
                msg = "Your walk was clearly visible. Low sensitivity catches it with the fewest false alarms."
            elif rec == "medium":
                msg = "Low missed your walk; Medium caught it."
            else:
                msg = "Only High caught your walk. Expect a few more false alarms at this setting."
            w["result"] = {
                "duration_s": round(now() - w["started"], 1),
                "peak_score": round(max(scores), 3),
                "median_score": round(statistics.median(scores), 3),
                "idle_median": round(statistics.median(idle), 3),
                "threshold_by_level": dict(THRESHOLDS),
                "detected_by_level": detected,
                "recommended": rec,
                "message": msg,
            }
            w["running"] = False
            return self.walk_view()

    # ---------- queries ----------
    def history(self, seconds, points):
        t1 = now()
        t0 = t1 - seconds
        points = max(10, min(5000, points))
        step = seconds / points
        buckets = {}
        with self.lock:
            samples = [s for s in self.samples if s[0] >= t0]
        for (t, s, thr, per) in samples:
            if s is None:
                continue
            i = int((t - t0) / step)
            b = buckets.get(i)
            if b is None:
                buckets[i] = [t, s, thr, dict(per)]
            else:
                b[0] = t
                b[1] = max(b[1], s)
                b[2] = thr
                for k, v in per.items():
                    b[3][k] = max(b[3].get(k, 0.0), v)
        keys = sorted(buckets)
        srcs = set(self.sources)
        out = {"t": [], "score": [], "threshold": [], "per_source": {k: [] for k in srcs}}
        for i in keys:
            b = buckets[i]
            out["t"].append(round(b[0], 3))
            out["score"].append(round(b[1], 3))
            out["threshold"].append(b[2])
            for k in srcs:
                out["per_source"][k].append(b[3].get(k))
        return out

    def list_events(self, limit, before):
        with self.lock:
            evs = [e for e in reversed(self.events) if before is None or e["id"] < before]
            return [dict(e) for e in evs[:limit]]


SIM = None
LIVE_CONNS = set()
LIVE_LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wifi-sentry-mock"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def setup(self):
        super().setup()
        with LIVE_LOCK:
            LIVE_CONNS.add(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            with LIVE_LOCK:
                LIVE_CONNS.discard(self.connection)

    def log_message(self, fmt, *args):
        if SIM.args.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == "/api/status":
            return self._json(SIM.status())
        if p == "/api/events":
            limit = int(q.get("limit", ["50"])[0])
            before = q.get("before", [None])[0]
            before = int(before) if before not in (None, "") else None
            return self._json({"events": SIM.list_events(limit, before)})
        if p == "/api/history":
            seconds = float(q.get("seconds", ["3600"])[0])
            points = int(q.get("points", ["600"])[0])
            return self._json(SIM.history(seconds, points))
        if p == "/api/walktest":
            return self._json(SIM.walk_view())
        if p == "/api/stream":
            return self._stream()
        if p.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if p.startswith("/dev/"):
            return self._json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._body()
        if body is None:
            return self._json({"error": "bad json"}, 400)
        if p == "/api/arm":
            with SIM.lock:
                SIM.armed = bool(body.get("armed"))
            st = SIM.status()
            SIM.publish("status", st)
            return self._json(st)
        if p == "/api/sensitivity":
            lvl = body.get("level")
            if lvl not in THRESHOLDS:
                return self._json({"error": "level must be low, medium or high"}, 400)
            with SIM.lock:
                SIM.sensitivity = lvl
            st = SIM.status()
            SIM.publish("status", st)
            return self._json(st)
        if p == "/api/relearn":
            SIM.start_learning(SIM.args.learn_seconds)
            st = SIM.status()
            SIM.publish("status", st)
            return self._json(st)
        if p == "/api/walktest":
            act = body.get("action")
            if act == "start":
                return self._json(SIM.start_walk())
            if act == "stop":
                return self._json(SIM.stop_walk())
            return self._json({"error": "action must be start or stop"}, 400)
        if p.startswith("/api/events/") and p.endswith("/label"):
            try:
                eid = int(p.split("/")[3])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            lab = body.get("label")
            if lab not in ("real", "false", None):
                return self._json({"error": "bad label"}, 400)
            with SIM.lock:
                for e in SIM.events:
                    if e["id"] == eid:
                        e["label"] = lab
                        if "note" in body:
                            e["note"] = str(body.get("note") or "")
                        return self._json(dict(e))
            return self._json({"error": "no such event"}, 404)
        if p == "/_mock/set":
            if "burst" in body:
                SIM.add_burst(float(body["burst"]), amp=body.get("amp"))
            if "offline" in body:
                with SIM.lock:
                    SIM.mac_forced_offline = bool(body["offline"])
                    SIM.sources["mac"]["online"] = not SIM.mac_forced_offline
                    SIM._update_state(now())
                    SIM.publish("status", SIM.status())
            if "learning" in body:
                SIM.start_learning(float(body["learning"]))
                SIM.publish("status", SIM.status())
            if "esp32" in body:
                with SIM.lock:
                    if body["esp32"]:
                        SIM._add_esp32()
                    else:
                        SIM.sources.pop("esp32-hall", None)
                    SIM.publish("status", SIM.status())
            if "freeze" in body:
                SIM.frozen_until = now() + float(body["freeze"])
            if "die" in body:
                threading.Thread(target=CONTROL.die, args=(float(body["die"]),), daemon=True).start()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = SIM.subscribe()
        epoch = SIM.server_epoch
        self.close_connection = True
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.write(("event: status\ndata: %s\n\n" % json.dumps(SIM.status())).encode())
            self.wfile.flush()
            while SIM.server_epoch == epoch:
                try:
                    msg = q.get(timeout=1.0)
                except queue.Empty:
                    self.wfile.write(b": ka\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            SIM.unsubscribe(q)


class Control:
    """Runs the HTTP server and can kill/restart it to simulate an engine restart."""

    def __init__(self, port):
        self.port = port
        self.server = None
        self.lock = threading.Lock()
        self.up = threading.Event()

    def start(self):
        with self.lock:
            ThreadingHTTPServer.allow_reuse_address = True
            ThreadingHTTPServer.daemon_threads = True
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
            threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                             daemon=True).start()
            self.up.set()

    def die(self, down_for):
        with self.lock:
            if not self.server:
                return
            print("[mock] server going down for %.0f s" % down_for, flush=True)
            SIM.server_epoch += 1
            srv = self.server
            self.server = None
            self.up.clear()
        srv.shutdown()
        srv.server_close()
        # a real engine dying drops every socket, including idle keep-alive ones
        with LIVE_LOCK:
            conns = list(LIVE_CONNS)
            LIVE_CONNS.clear()
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        time.sleep(down_for)
        print("[mock] server back up", flush=True)
        self.start()


CONTROL = None


def main():
    global SIM, CONTROL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8451)
    ap.add_argument("--scenario", default="demo",
                    choices=["demo", "idle", "learning", "motion", "offline", "esp32", "flaky"])
    ap.add_argument("--motion-every", type=float, default=None,
                    help="start a motion burst every N seconds (demo default 60, motion default 30)")
    ap.add_argument("--esp32", action="store_true", help="add a second esp32 source")
    ap.add_argument("--die-every", type=float, default=None, help="kill the server every N seconds")
    ap.add_argument("--down-for", type=float, default=10.0, help="how long the server stays dead")
    ap.add_argument("--learn-seconds", type=float, default=90.0)
    ap.add_argument("--offline-cycle", type=float, default=60.0,
                    help="offline scenario: Mac source flips online/offline every N s (0 = stay offline)")
    ap.add_argument("--walk-amp", type=float, default=None, help="fix the walk-test signal amplitude")
    ap.add_argument("--many-events", type=int, default=0,
                    help="seed N extra past events (to exercise pagination)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.motion_every is None:
        args.motion_every = {"demo": 60.0, "motion": 30.0, "esp32": 45.0, "flaky": 45.0}.get(args.scenario)
    if args.scenario == "esp32":
        args.esp32 = True
    if args.scenario == "flaky" and args.die_every is None:
        args.die_every = 40.0
        args.down_for = 12.0

    SIM = Sim(args)
    CONTROL = Control(args.port)
    CONTROL.start()
    print("[mock] wifi-sentry mock on http://127.0.0.1:%d  scenario=%s" % (args.port, args.scenario),
          flush=True)

    def ticker():
        nxt = time.time()
        while True:
            nxt += 1.0
            try:
                SIM.tick()
            except Exception as e:  # keep the sim alive
                print("[mock] tick error:", repr(e), flush=True)
            time.sleep(max(0.0, nxt - time.time()))

    threading.Thread(target=ticker, daemon=True).start()

    try:
        if args.die_every:
            while True:
                time.sleep(args.die_every)
                CONTROL.die(args.down_for)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[mock] bye")


if __name__ == "__main__":
    main()
