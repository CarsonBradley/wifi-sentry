"""Live inputs: the wifisense child process (Mac) and the ESP32 UDP listener."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import signal
import socket
import time
from collections.abc import Callable

log = logging.getLogger("sentry.sources")

Callback = Callable[[str, str, dict, float, str], None]  # (src, kind, payload, t, raw_line)


class MacSource:
    """Spawns `wifisense`, parses its JSONL, restarts it with exponential backoff."""

    def __init__(self, command: list[str], cwd: str, on_sample: Callback, restart_min: float = 1.0,
                 restart_max: float = 60.0):
        self.command = command
        self.cwd = cwd
        self.on_sample = on_sample
        self.restart_min = restart_min
        self.restart_max = restart_max
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.restarts = 0
        self.last_error: str | None = None
        self._stopping = False

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="mac-source")

    async def _run(self) -> None:
        backoff = self.restart_min
        while not self._stopping:
            started = time.monotonic()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.command, cwd=self.cwd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True, limit=1 << 20)
                log.info("wifisense started pid=%s: %s", self.proc.pid, " ".join(self.command))
                err_task = asyncio.create_task(self._stderr(self.proc))
                await self._stdout(self.proc)
                rc = await self.proc.wait()
                err_task.cancel()
                if self._stopping:
                    break
                self.last_error = f"wifisense exited rc={rc}"
                log.warning("%s", self.last_error)
            except FileNotFoundError as e:
                self.last_error = f"wifisense not found: {e}"
                log.error("%s", self.last_error)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                self.last_error = f"wifisense failed: {e!r}"
                log.exception("wifisense failed")
            finally:
                await self._kill()
            if self._stopping:
                break
            if time.monotonic() - started > 60:
                backoff = self.restart_min
            self.restarts += 1
            log.info("restarting wifisense in %.0f s", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(self.restart_max, backoff * 2)

    async def _stdout(self, proc) -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                s = line.decode("utf-8", "replace").strip()
                obj = json.loads(s)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            t = obj.get("t")
            if not isinstance(t, (int, float)):
                t = time.time()
            src = obj.get("src") if isinstance(obj.get("src"), str) else "mac"
            try:
                self.on_sample(src, "mac", obj, float(t), s)
            except Exception:  # noqa: BLE001
                log.exception("mac sample handler failed")

    async def _stderr(self, proc) -> None:
        assert proc.stderr is not None
        last = 0.0
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            now = time.monotonic()
            if now - last > 10:  # rate-limit chatter
                last = now
                log.info("wifisense: %s", line.decode("utf-8", "replace").rstrip())

    async def _kill(self) -> None:
        p = self.proc
        if p is None or p.returncode is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                p.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(p.wait(), 3.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await p.wait()

    async def stop(self) -> None:
        self._stopping = True
        await self._kill()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


MAX_DATAGRAM = 1400
MAX_ESP32_SOURCES = 16    # board names holding a slot at once
MAX_ESP32_NAMES = 64      # distinct names per engine run: the detector keeps a scorer for every name it saw
ESP32_EXPIRE_S = 600.0    # a name with no accepted datagram for 10 min frees its slot and its sender-IP pin
MAX_CSI_VALUES = 640      # largest firmware layout is 490 values (esp32/README.md)
_NUM_FIELDS = ("rssi", "noise", "channel", "seq", "t_us")
SRC_RE = re.compile(r"esp32:[A-Za-z0-9_.-]{1,31}")  # the firmware's node-name rule (esp32/README.md)

# Optional shared-key signature (esp32/README.md section 6): the board appends ,"sig":"<32 hex>" just
# before the closing brace of the datagram it would otherwise send. sig = the first 16 bytes of
# HMAC-SHA256(key, that unsigned datagram), lowercase hex, so neither side re-serialises any JSON.
SIG_HEX = 32
SIG_TAIL_LEN = len(',"sig":"') + SIG_HEX + len('"}')
_SIG_TAIL = re.compile(rb',"sig":"([0-9a-f]{32})"\}\Z')
REPLAY_SLACK_S = 2.0      # network delay / jitter the replay guard tolerates
REPLAY_DRIFT = 1e-3       # how fast a board's boot time (on our clock) may creep earlier: 25x crystal tolerance
REPLAY_RESYNC_S = 30.0    # a board silent this long is re-anchored by its next signed datagram


def esp32_sig(key: bytes, unsigned: bytes) -> bytes:
    """The "sig" value (ASCII hex bytes) for an unsigned datagram."""
    return hmac.new(key, unsigned, hashlib.sha256).hexdigest()[:SIG_HEX].encode()


def split_sig(data: bytes) -> tuple[bytes, bytes] | None:
    """(the unsigned datagram that was signed, sig) if data ends with a sig member, else None."""
    m = _SIG_TAIL.search(data, max(0, len(data) - SIG_TAIL_LEN))
    return None if m is None else (data[:m.start()] + b"}", m.group(1))


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


def _finite_float(s: str) -> float:
    x = float(s)
    if not math.isfinite(x):
        raise ValueError("non-finite number")
    return x


def _real(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_esp32(obj) -> str | None:
    """Return why a decoded datagram is unusable, or None if it is well-formed enough to ingest.
    Anything that reaches the detector must be numeric where numbers are expected: a stray string,
    bool or huge value would otherwise raise inside the scorer or leak NaN/inf into the status JSON."""
    if not isinstance(obj, dict) or obj.get("v") != 1:
        return "not a v1 object"
    src = obj.get("src")
    if not isinstance(src, str) or not SRC_RE.fullmatch(src):
        return "bad src"
    csi = obj.get("csi")
    if not isinstance(csi, list) or len(csi) > MAX_CSI_VALUES \
            or not all(_real(v) and -1024 <= v <= 1024 for v in csi):
        return "bad csi"
    for k in _NUM_FIELDS:
        v = obj.get(k)
        if v is not None and not (_real(v) and abs(v) < 2 ** 63):
            return f"bad {k}"
    if obj.get("fmt") is not None and (not isinstance(obj["fmt"], str) or len(obj["fmt"]) > 32):
        return "bad fmt"
    return None


class _Board:
    """One claimed board name: the sender it is pinned to and the replay-guard state."""
    __slots__ = ("ip", "last", "t_us", "rx", "boot")

    def __init__(self, ip: str, now: float):
        self.ip = ip
        self.last = now           # last accepted datagram (monotonic)
        self.t_us: int | None = None
        self.rx = 0.0             # when t_us was accepted (monotonic)
        self.boot = 0.0           # estimated boot time of the board on our clock (now - t_us)


class Esp32Protocol(asyncio.DatagramProtocol):
    """One JSON CSI datagram per frame (esp32/README.md section 6). Sources auto-register by src.

    Malformed, oversized or non-numeric datagrams are dropped and counted. At most MAX_ESP32_SOURCES
    names hold a slot at once; a name is pinned to the first IP that sent it and freed (slot and pin)
    after ESP32_EXPIRE_S without an accepted datagram. Without a key the port is unauthenticated. With
    a key, only correctly signed datagrams are accepted, and each board's uptime (t_us) has to move
    forward in step with our clock, so recorded datagrams can't be replayed while the board is live."""

    def __init__(self, on_sample: Callback, key: str | bytes = "", on_expire: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.on_sample = on_sample
        self.key = key.encode() if isinstance(key, str) else bytes(key or b"")
        self.on_expire = on_expire  # e.g. drop the detector's state for a name whose slot expired
        self.clock = clock
        self.invalid = 0            # every dropped datagram, whatever the reason
        self.received = 0
        self.refused_sources = 0    # new names refused: all slots taken, or MAX_ESP32_NAMES reached
        self.unsigned = 0           # no sig while a key is set
        self.bad_sig = 0
        self.replayed = 0           # signed, but t_us out of step (replay, or reordered in transit)
        self.wrong_ip = 0           # a pinned name from another sender
        self.expired = 0
        self.sources: dict[str, _Board] = {}
        self.names_seen: set[str] = set()
        self._warned: set = set()
        self._last_warn = 0.0
        self._last_sweep = 0.0
        self._hinted = False

    def _bad(self, why: str, addr) -> None:
        self.invalid += 1
        now = time.monotonic()
        if now - self._last_warn > 60:  # one line a minute at most, whatever the flood rate
            self._last_warn = now
            log.warning("dropped invalid ESP32 datagram from %s (%s); %d invalid so far", addr[0], why,
                        self.invalid)

    def _warn_once(self, key, msg: str, *args) -> None:
        if key not in self._warned:
            if len(self._warned) >= 256:  # bounded, whatever an attacker sends
                self._warned.clear()
            self._warned.add(key)
            log.warning(msg, *args)

    def _expire(self, src: str) -> None:
        del self.sources[src]
        self.expired += 1
        log.info("ESP32 source %s silent for %.0f min: slot and address pin released", src, ESP32_EXPIRE_S / 60)
        if self.on_expire is not None:
            try:
                self.on_expire(src)
            except Exception:  # noqa: BLE001
                log.exception("esp32 expire handler failed")

    def _sweep(self, now: float) -> None:
        self._last_sweep = now
        for src in [s for s, b in self.sources.items() if now - b.last > ESP32_EXPIRE_S]:
            self._expire(src)

    def _fresh(self, b: _Board, tus, now: float) -> bool:
        """Replay guard for signed datagrams. t_us is the board's uptime; a reboot restarts it at 0."""
        if not isinstance(tus, int) or isinstance(tus, bool) or tus < 0:
            return False
        boot = now - tus / 1e6
        if b.t_us is None or now - b.rx > REPLAY_RESYNC_S:
            b.boot = boot                       # first datagram, or long silence: (re)anchor
        elif tus > b.t_us:
            # Forward, but not ahead of real time: a recording from an earlier, longer boot claims an
            # earlier boot time. Our estimate may only creep earlier slowly (clock drift, less delay).
            if boot < b.boot - REPLAY_SLACK_S:
                return False
            b.boot = max(min(b.boot, boot), b.boot - REPLAY_DRIFT * (now - b.rx))
        elif tus / 1e6 > now - b.rx + REPLAY_SLACK_S:
            return False                        # "rebooted" before its last datagram reached us: a replay
        else:
            b.boot = boot                       # a genuine reboot
        b.t_us, b.rx = tus, now
        return True

    def datagram_received(self, data: bytes, addr) -> None:
        t = time.time()
        now = self.clock()
        if len(data) > MAX_DATAGRAM:
            self._bad("oversized", addr)
            return
        signed = split_sig(data)
        if self.key:  # authenticate before parsing: an unauthenticated flood costs an HMAC, not a JSON decode
            if signed is None:
                self.unsigned += 1
                self._bad("unsigned, [esp32] key is set", addr)
                return
            if not hmac.compare_digest(esp32_sig(self.key, signed[0]), signed[1]):
                self.bad_sig += 1
                self._bad("bad signature", addr)
                return
        elif signed is not None and not self._hinted:
            self._hinted = True
            log.warning("ESP32 datagrams from %s are signed but [esp32] key is not set: signatures are not checked",
                        addr[0])
        try:
            obj = json.loads(data.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)
        except (UnicodeDecodeError, ValueError, RecursionError):  # JSONDecodeError is a ValueError
            self._bad("not JSON", addr)
            return
        why = validate_esp32(obj)
        if why:
            self._bad(why, addr)
            return
        obj.pop("sig", None)
        src = obj["src"]
        if now - self._last_sweep > 10:
            self._sweep(now)
        b = self.sources.get(src)
        if b is not None and now - b.last > ESP32_EXPIRE_S:
            self._expire(src)
            b = None
        if b is None:
            if len(self.sources) >= MAX_ESP32_SOURCES:
                self._sweep(now)
            if len(self.sources) >= MAX_ESP32_SOURCES or (
                    src not in self.names_seen and len(self.names_seen) >= MAX_ESP32_NAMES):
                self.refused_sources += 1
                self._bad(f"too many sources, refusing {src!r}", addr)
                return
            b = self.sources[src] = _Board(addr[0], now)
            self.names_seen.add(src)
        elif addr[0] != b.ip:
            # A board keeps its DHCP address; another sender using its name is spoofing (or the board
            # got a new address: it is accepted once the old pin expires).
            self.wrong_ip += 1
            self.invalid += 1
            self._warn_once((src, addr[0]), "dropping ESP32 datagrams for %s from %s: that name is pinned to %s "
                            "until it is silent for %.0f min or the engine restarts", src, addr[0], b.ip,
                            ESP32_EXPIRE_S / 60)
            return
        if self.key and not self._fresh(b, obj.get("t_us"), now):
            self.replayed += 1
            self._bad("replayed or out-of-order signed datagram", addr)
            return
        b.last = now
        self.received += 1
        obj["rx_t"] = round(t, 4)
        obj["rx_from"] = addr[0]
        try:
            self.on_sample(src, "esp32", obj, t, None)
        except Exception:  # noqa: BLE001
            log.exception("esp32 sample handler failed")

    def counters(self) -> dict:
        """For /api/debug."""
        return {k: getattr(self, k) for k in ("received", "invalid", "refused_sources", "unsigned", "bad_sig",
                                              "replayed", "wrong_ip", "expired")} | {
            "signed_only": bool(self.key), "sources": sorted(self.sources)}

    def error_received(self, exc) -> None:  # e.g. ICMP port unreachable on some platforms
        log.debug("esp32 socket error: %s", exc)


async def start_esp32(bind: str, port: int, on_sample: Callback, key: str = "",
                      on_expire: Callable[[str], None] | None = None):
    loop = asyncio.get_running_loop()
    key = str(key or "")
    if key and len(key) < 16:
        log.warning("[esp32] key is only %d characters; use at least 16 random ones (README: esp32)", len(key))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    sock.bind((bind, port))
    transport, proto = await loop.create_datagram_endpoint(
        lambda: Esp32Protocol(on_sample, key=key, on_expire=on_expire), sock=sock)
    log.info("ESP32 UDP listener on %s:%d (%s)", bind, port,
             "signed datagrams only" if key else "unauthenticated: no [esp32] key set")
    return transport, proto
