"""Alert channels ("sinks"). Only events opened while ARMED (and not during a walk test) reach them.

The dashboard is not a sink here: it always receives every event via the event list and the SSE
stream (app.py). External channels, all off until configured (dashboard or config file):

  * NtfySink    - push to the ntfy app (https://ntfy.sh or a self-hosted server)
  * WebhookSink - POST JSON (generic payload below, or Slack / Discord incoming-webhook shapes)
  * EmailSink   - SMTP with STARTTLS, SSL or plain, sent in a worker thread

Delivery never blocks the detector and never raises into it: every send runs in its own task with a
timeout and up to `attempts` tries (backoff between them; no retry for answers that can't improve,
such as a refused login or HTTP 4xx other than 408/429). The result of the last attempt per channel is
kept (`AlertRouter.results`) and reported to the engine through `on_result`.

Rate limiting: one alert per event; after an alert, further events within `cooldown_s` are not sent
but counted, and the next alert says how many were held back. Cooldowns are kept per sensor class
("mac", "esp32"): an event seen only by ESP32 boards (whose UDP data could be forged) never uses up the
Mac sensor's cooldown, and an open event that a new sensor class joins is alerted again.

"Not watching" alerts (kind "blind", "watching", "restart") have their own cooldown, so a sensor
outage while armed is reported even right after a motion alert, and flapping can't flood the phone.

Errors shown on the dashboard name the target as host[:port] only: URLs (webhook URLs, ntfy topics,
user:password@ parts) are secrets and are never echoed into results, logs, the API or the database.

Generic webhook payload (format "json"):
  {"source": "wifi-sentry", "kind": "open" | "close" | "test" | "blind" | "watching" | "restart",
   "title": str, "message": str,
   "time": ISO-8601 local time, "url": dashboard link or null, "suppressed": int,
   "event": {id, start, end, duration_s, peak_score, threshold, sources, armed, ...} or null,
   "status": {state, armed, score, threshold, sensitivity}}
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import smtplib
import socket
import ssl
import time
from collections import deque
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger("sentry.sinks")

HTTP_TIMEOUT_S = 10.0
SMTP_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------- message text
def strength(ev: dict) -> tuple[str, float | None]:
    """Same rating as the dashboard: peak relative to the trigger level in force at the peak."""
    try:
        ratio = float(ev["peak_score"]) / float(ev["threshold"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return "", None
    return ("slight" if ratio < 1.6 else "moderate" if ratio < 3 else "strong"), ratio


def _clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def _dur(s: float) -> str:
    s = max(0, int(round(s)))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s and m < 10 else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def compose(kind: str, event: dict | None, *, suppressed: int = 0, cooldown_s: float = 0,
            base_url: str = "", now: float | None = None) -> dict:
    """The alert for one event. Deliberately generic: no names, addresses or sensor details, since
    the text may pass through a third-party push server."""
    now = time.time() if now is None else now
    ev = event or {}
    if kind == "test":
        title = "wifi-sentry test alert"
        lines = ["This is a test. If you can read this, wifi-sentry alerts reach you."]
    elif kind == "open":
        title = "Motion detected at home"
        word, ratio = strength(ev)
        lines = [f"Motion started at {_clock(ev.get('start', now))}."]
        if word:
            lines.append(f"Strength: {word} ({ratio:.1f}x the trigger level).")
        lines.append(f"Going on for {_dur(ev.get('duration_s') or now - ev.get('start', now))} so far.")
        if suppressed:
            lines.append(f"{suppressed} more motion event{'s' if suppressed != 1 else ''} since the previous "
                         f"alert {'were' if suppressed != 1 else 'was'} not sent separately "
                         f"(alerts are at least {_dur(cooldown_s)} apart).")
    elif kind == "close":
        title = "Motion ended at home"
        word, _ = strength(ev)
        end = ev.get("end") or now
        lines = [f"The motion that started at {_clock(ev.get('start', end))} ended at {_clock(end)}, "
                 f"after {_dur(ev.get('duration_s') or end - ev.get('start', end))}."]
        if word:
            lines.append(f"Peak strength: {word}.")
    elif kind == "blind":
        title = "wifi-sentry can't sense"
        lines = [f"Not watching since {_clock(ev.get('since', now))}: {ev.get('reason') or 'no sensor data'}.",
                 "Motion is not detected until this clears; you'll get a message when it watches again."]
    elif kind == "watching":
        title = "wifi-sentry is watching again"
        lines = [f"Watching again since {_clock(ev.get('until', now))}, after "
                 f"{_dur(ev.get('until', now) - ev.get('since', now))} without sensing."]
    elif kind == "restart":
        title = "wifi-sentry was not running"
        lines = [f"Nothing was watched from {_clock(ev.get('since', now))} to {_clock(ev.get('until', now))} "
                 f"({_dur(ev.get('until', now) - ev.get('since', now))}): the engine wasn't running or the Mac "
                 "was asleep. It is running again."]
    else:
        raise ValueError(f"unknown alert kind {kind!r}")
    url = base_url.strip()
    if url:
        lines.append(f"Dashboard: {url}")
    return {"kind": kind, "title": title, "message": "\n".join(lines), "url": url or None,
            "time": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "suppressed": suppressed if kind == "open" else 0,
            "event": event if kind in ("open", "close") else None}


HEALTH_KINDS = ("blind", "watching", "restart")


# ---------------------------------------------------------------------- channels
# EHLO name sent to SMTP servers. An address literal instead of smtplib's default
# socket.getfqdn(): that lookup can block for tens of seconds on Macs whose hostname
# has no reverse DNS, and it would send the computer's name to the mail server.
_EHLO_NAME = "[127.0.0.1]"

class DeliveryError(Exception):
    """A delivery failure with a message meant for the dashboard. retry=False: trying again won't help."""

    def __init__(self, msg: str, retry: bool = True):
        super().__init__(msg)
        self.retry = retry


def _host(url: str) -> str:
    """host[:port] of a URL, never its user:password@ part, path or query (those can be secrets)."""
    try:
        u = urlsplit(url)
        h = u.hostname or "?"
        h = f"[{h}]" if ":" in h else h
        return f"{h}:{u.port}" if u.port else h
    except ValueError:
        return "?"


_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")


def scrub(text):
    """Replace any URL in a stored or displayed message by its host[:port] (used on results persisted by
    older versions, whose errors could echo the full URL)."""
    if not isinstance(text, str):
        return text
    return _URL_RE.sub(lambda m: _host(m.group(0)) + "/…", text)


# Targets the engine refuses to post to: loopback, link-local (cloud metadata), unspecified, multicast,
# and the engine's own listening addresses (set by the engine). Tests turn the loopback rule off.
ALLOW_LOOPBACK_TARGETS = False
SELF_BOUND: set[tuple[str, int]] = set()
HTTP_REASONS = {400: "bad request", 401: "login or token refused", 403: "refused (forbidden; check the token or "
                "topic)", 404: "not found (check the URL or topic)", 405: "method not allowed",
                408: "request timeout", 413: "message too large", 429: "rate limited, too many messages"}


def refused_address(ip: str, port: int) -> str | None:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return None
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    if (a.compressed, port) in SELF_BOUND or (a.is_unspecified and any(p == port for _, p in SELF_BOUND)):
        return "that is wifi-sentry itself"
    if a.is_link_local or a.is_multicast or a.is_unspecified:
        return "link-local / multicast addresses are refused"
    if a.is_loopback and not ALLOW_LOOPBACK_TARGETS:
        return "this Mac's own loopback address is refused"
    return None


async def check_target(url: str) -> None:
    """Refuse a target that resolves to an address the engine must not post to (SSRF to this Mac)."""
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise DeliveryError("not a valid URL", retry=False) from None
    if not host or u.scheme not in ("http", "https"):
        raise DeliveryError("not a valid http(s) URL", retry=False)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise DeliveryError(f"couldn't resolve {_host(url)}") from None
    for info in infos:
        why = refused_address(info[4][0], port)
        if why:
            raise DeliveryError(f"{_host(url)}: {why}", retry=False)


async def _post(url: str, timeout: float, **kw) -> None:
    host = _host(url)
    await check_target(url)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(url, allow_redirects=False, **kw) as r:
                if r.status < 300:
                    return
                # Status and a generic reason only: never echo the response body (it may be anything,
                # from anywhere the URL points).
                reason = HTTP_REASONS.get(r.status) or ("server error" if r.status >= 500 else
                                                        "redirect (not followed)" if r.status < 400 else "refused")
                raise DeliveryError(f"{host} answered HTTP {r.status}: {reason}",
                                    retry=r.status >= 500 or r.status in (408, 429))
    except DeliveryError:
        raise
    except asyncio.TimeoutError:
        raise DeliveryError(f"no answer from {host} within {timeout:.0f} s") from None
    except aiohttp.InvalidURL:
        raise DeliveryError("not a valid URL", retry=False) from None
    except aiohttp.ClientConnectorError as e:
        raise DeliveryError(f"couldn't connect to {host}: "
                            f"{(e.os_error.strerror if e.os_error else None) or 'connection failed'}") from None
    except aiohttp.ClientError as e:
        raise DeliveryError(f"{host}: {e.__class__.__name__}") from None


class Sink:
    id = "sink"
    label = "Alert channel"
    timeout = HTTP_TIMEOUT_S

    async def send(self, alert: dict, status: dict) -> None:
        raise NotImplementedError

    async def close(self) -> None:  # kept for callers of the 0.2 API
        pass


class NtfySink(Sink):
    id = "ntfy"
    label = "Phone (ntfy)"

    def __init__(self, url: str, priority: str = "high", token: str = "", timeout: float = HTTP_TIMEOUT_S):
        self.url, self.priority, self.token, self.timeout = url, priority or "high", token or "", timeout

    def headers(self, alert: dict) -> dict:
        kind = alert["kind"]
        h = {"Title": alert["title"],
             "Priority": self.priority if kind in ("open", "blind", "restart", "test") else "default",
             "Tags": {"open": "rotating_light", "close": "white_check_mark", "test": "bell", "blind": "warning",
                      "watching": "white_check_mark", "restart": "warning"}[kind]}
        if alert.get("url") and alert["url"].isascii():
            h["Click"] = alert["url"]
        if self.token:
            if ":" in self.token:   # user:password
                h["Authorization"] = "Basic " + base64.b64encode(self.token.encode()).decode()
            else:
                h["Authorization"] = f"Bearer {self.token}"
        return h

    async def send(self, alert, status):
        await _post(self.url, self.timeout, data=alert["message"].encode(), headers=self.headers(alert))


class WebhookSink(Sink):
    id = "webhook"
    label = "Webhook"
    FORMATS = ("json", "slack", "discord")

    def __init__(self, url: str, fmt: str = "json", timeout: float = HTTP_TIMEOUT_S):
        self.url, self.fmt, self.timeout = url, fmt if fmt in self.FORMATS else "json", timeout

    def payload(self, alert: dict, status: dict) -> dict:
        text = f"{alert['title']}\n{alert['message']}"
        if self.fmt == "slack":
            return {"text": text}
        if self.fmt == "discord":
            return {"content": text[:2000]}
        return {"source": "wifi-sentry", "kind": alert["kind"], "title": alert["title"], "message": alert["message"],
                "time": alert["time"], "url": alert.get("url"), "suppressed": alert.get("suppressed", 0),
                "event": alert.get("event"),
                "status": {k: status.get(k) for k in ("state", "armed", "score", "threshold", "sensitivity")}}

    async def send(self, alert, status):
        await _post(self.url, self.timeout, json=self.payload(alert, status))


class EmailSink(Sink):
    id = "email"
    label = "Email"
    SECURITY = ("starttls", "ssl", "none")

    def __init__(self, host, port, security="starttls", username="", password="", sender="", to=(),
                 timeout: float = SMTP_TIMEOUT_S):
        if isinstance(security, bool):  # 0.2 signature: starttls flag
            security = "starttls" if security else "none"
        self.host, self.port, self.security = host, int(port), security
        self.username, self.password, self.sender = username, password, sender
        self.to = [to] if isinstance(to, str) else list(to)
        self.timeout = timeout

    def message(self, alert: dict) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = alert["title"], self.sender, ", ".join(self.to)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="wifi-sentry.local")
        msg.set_content(alert["message"] + "\n\n-- \nwifi-sentry\n")
        return msg

    def _send_sync(self, alert: dict) -> None:
        where = f"{self.host}:{self.port}"
        try:
            if self.security == "ssl":
                s = smtplib.SMTP_SSL(self.host, self.port, local_hostname=_EHLO_NAME, timeout=self.timeout,
                                     context=ssl.create_default_context())
            else:
                s = smtplib.SMTP(self.host, self.port, local_hostname=_EHLO_NAME, timeout=self.timeout)
            with s:
                if self.security == "starttls":
                    s.starttls(context=ssl.create_default_context())
                if self.username:
                    s.login(self.username, self.password)
                s.send_message(self.message(alert))
        except smtplib.SMTPAuthenticationError as e:
            raise DeliveryError(f"{where} refused the login: {e.smtp_code} {_smtp_text(e.smtp_error)}", retry=False)
        except smtplib.SMTPNotSupportedError as e:
            raise DeliveryError(f"{where}: {e} (try another security setting)", retry=False)
        except smtplib.SMTPRecipientsRefused as e:
            bad = "; ".join(f"{a}: {c} {_smtp_text(m)}" for a, (c, m) in e.recipients.items())
            raise DeliveryError(f"{where} refused the recipient: {bad}", retry=False)
        except smtplib.SMTPResponseException as e:
            raise DeliveryError(f"{where} answered {e.smtp_code} {_smtp_text(e.smtp_error)}", retry=e.smtp_code < 500)
        except ssl.SSLError as e:
            raise DeliveryError(f"{where}: TLS failed ({e.reason or e}); check the port and security setting",
                                retry=False)
        except (socket.timeout, TimeoutError):
            raise DeliveryError(f"no answer from {where} within {self.timeout:.0f} s")
        except smtplib.SMTPException as e:
            raise DeliveryError(f"{where}: {e}")
        except OSError as e:
            raise DeliveryError(f"couldn't connect to {where}: {e.strerror or e}")

    async def send(self, alert, status):
        await asyncio.to_thread(self._send_sync, alert)


def _smtp_text(b) -> str:
    return (b.decode(errors="replace") if isinstance(b, bytes) else str(b)).replace("\n", " ").strip()


def build_sinks(cfg: dict) -> list[Sink]:
    """Sinks from the config file alone ([alerts.*] in config.toml / config.local.toml). The engine
    uses alertcfg.AlertConfig, which lets dashboard settings replace these per channel."""
    from .alertcfg import AlertConfig
    return AlertConfig(None, cfg).build_sinks()


# ---------------------------------------------------------------------- delivery
async def deliver(sink: Sink, alert: dict, status: dict, attempts: int = 3,
                  backoff: tuple[float, ...] = (2.0, 8.0)) -> dict:
    """Send with retries. Never raises; returns {ok, attempts, error, ms}."""
    t0 = time.monotonic()
    err, n = None, 0
    for n in range(1, max(1, attempts) + 1):
        retry = True
        try:
            await asyncio.wait_for(sink.send(alert, status), sink.timeout + 5)
            return {"ok": True, "attempts": n, "error": None, "ms": round((time.monotonic() - t0) * 1000)}
        except DeliveryError as e:
            err, retry = str(e), e.retry
        except asyncio.TimeoutError:
            err = f"no answer within {sink.timeout + 5:.0f} s"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a broken channel must never take the engine down
            err = f"{e.__class__.__name__}: {e}"
        if not retry or n >= attempts:
            break
        await asyncio.sleep(backoff[min(n - 1, len(backoff) - 1)] if backoff else 0)
    return {"ok": False, "attempts": n, "error": err, "ms": round((time.monotonic() - t0) * 1000)}


def source_class(sources) -> str:
    """Cooldown bucket of an event: "mac" if the Mac's own sensor saw it, else "esp32"."""
    return "mac" if any(str(x) == "mac" for x in (sources or [])) else "esp32"


class AlertRouter:
    """Fans armed events out to the channels with one alert per event and a cooldown per sensor class.
    Failures are recorded, never raised."""

    def __init__(self, sinks: list[Sink], cooldown_s: float = 300.0, notify_on_close: bool = False,
                 base_url: str = "", attempts: int = 3, backoff: tuple[float, ...] = (2.0, 8.0),
                 on_result=None, health_cooldown_s: float = 1800.0):
        self.sinks = list(sinks)
        self.cooldown = float(cooldown_s)
        self.health_cooldown = float(health_cooldown_s)
        self.notify_on_close = bool(notify_on_close)
        self.base_url = base_url or ""
        self.attempts = attempts
        self.backoff = backoff
        self.on_result = on_result
        self.last_sent_by: dict[str, float] = {}   # sensor class -> time of its last motion alert
        self.suppressed_by: dict[str, int] = {}    # sensor class -> armed events held back since then
        self.last_health = -1e18                   # last "can't sense" / "was not running" alert
        self.alerted: deque = deque(maxlen=500)
        self.alerted_info: dict[int, dict] = {}    # event id -> {"classes": [...], "peak": float}
        self.first_class: dict[int, str] = {}      # event id -> sensor class when it opened
        self.results: dict[str, dict] = {}  # channel id -> last result (any kind)
        self.last_ok: dict[str, float] = {}  # channel id -> time of the last successful delivery
        self.log: list[dict] = []           # last results, for /api/debug
        self._tasks: set[asyncio.Task] = set()

    # Aggregates kept for the API (0.2 field names).
    @property
    def last_sent(self) -> float:
        return max(self.last_sent_by.values(), default=-1e18)

    @property
    def suppressed(self) -> int:
        return sum(self.suppressed_by.values())

    def configure(self, sinks=None, cooldown_s=None, notify_on_close=None, base_url=None,
                  health_cooldown_s=None) -> None:
        if sinks is not None:
            self.sinks = list(sinks)
        if cooldown_s is not None:
            self.cooldown = float(cooldown_s)
        if notify_on_close is not None:
            self.notify_on_close = bool(notify_on_close)
        if base_url is not None:
            self.base_url = base_url
        if health_cooldown_s is not None:
            self.health_cooldown = float(health_cooldown_s)

    # -- persisted counters (so a restart doesn't reset the cooldown) ------------------------
    def export_state(self) -> dict:
        return {"v": 2, "last_sent_by": self.last_sent_by, "suppressed_by": self.suppressed_by,
                "last_health": self.last_health, "alerted": list(self.alerted)[-50:],
                "results": self.results, "last_ok": self.last_ok}

    def import_state(self, d: dict | None) -> None:
        if not isinstance(d, dict):
            return
        if "last_sent_by" in d:
            self.last_sent_by = {str(k): float(v) for k, v in (d.get("last_sent_by") or {}).items()
                                 if isinstance(v, (int, float))}
            self.suppressed_by = {str(k): int(v) for k, v in (d.get("suppressed_by") or {}).items()
                                  if isinstance(v, int)}
            self.last_health = float(d.get("last_health", self.last_health))
        elif "last_sent" in d:  # 0.2 / early 0.3 state: one shared cooldown, counted as the Mac's
            self.last_sent_by = {"mac": float(d.get("last_sent", -1e18))}
            self.suppressed_by = {"mac": int(d.get("suppressed", 0))}
        self.alerted.extend(int(x) for x in d.get("alerted", []) if isinstance(x, int))
        # Older versions could store full URLs (with secrets) in error texts: scrub on the way in.
        self.results = {k: dict(v, error=scrub(v.get("error"))) for k, v in (d.get("results") or {}).items()
                        if isinstance(v, dict)}
        self.last_ok = {k: float(v) for k, v in (d.get("last_ok") or {}).items() if isinstance(v, (int, float))}

    # -- events ---------------------------------------------------------------------------
    def handle(self, kind: str, event: dict, status: dict, now: float) -> bool:
        """kind: 'open' | 'update' | 'close'. Returns True when an alert was dispatched."""
        if not self.sinks or not event.get("armed") or event.get("walktest"):
            return False
        eid = event.get("id")
        cls = source_class(event.get("sources"))
        if kind in ("open", "update"):
            info = self.alerted_info.get(eid)
            if kind == "open":
                self.first_class[eid] = cls
                while len(self.first_class) > 200:
                    del self.first_class[next(iter(self.first_class))]
                if eid in self.alerted:
                    return False
            elif not (cls == "mac" and self.first_class.get(eid) == "esp32" and "mac" not in (info or {}).get("classes", [])):
                # The only update that alerts: the Mac's own sensor joining an event that ESP32 boards
                # opened (alerted or held back). Otherwise continuous forged ESP32 data could hold one
                # event open and swallow real motion.
                return False
            if now - self.last_sent_by.get(cls, -1e18) < self.cooldown:
                if kind == "open":
                    self.suppressed_by[cls] = self.suppressed_by.get(cls, 0) + 1
                    log.info("event #%s not alerted: %.0f s since the last %s alert (cooldown %.0f s); %d held back",
                             eid, now - self.last_sent_by[cls], cls, self.cooldown, self.suppressed_by[cls])
                return False
            if eid not in self.alerted:
                self.alerted.append(eid)
            prev = info["classes"] if info else []
            self.alerted_info[eid] = {"classes": sorted(set(prev) | {cls}), "peak": float(event.get("peak_score") or 0)}
            while len(self.alerted_info) > 200:
                del self.alerted_info[next(iter(self.alerted_info))]
            self.last_sent_by[cls] = now
            held = self.suppressed_by.pop(cls, 0)
            alert = compose("open", dict(event), suppressed=held, cooldown_s=self.cooldown,
                            base_url=self.base_url, now=now)
        elif kind == "close":
            self.alerted_info.pop(eid, None)
            self.first_class.pop(eid, None)
            if not self.notify_on_close or eid not in self.alerted:
                return False
            alert = compose("close", dict(event), base_url=self.base_url, now=now)
        else:
            return False
        return self._dispatch(alert, status, f"event #{eid}")

    def handle_health(self, kind: str, info: dict, status: dict, now: float) -> bool:
        """kind: 'blind' (armed and not watching), 'watching' (recovered after a sent 'blind'),
        'restart' (was not running while armed). Own cooldown; 'watching' only follows a sent alert,
        so it isn't rate limited."""
        if not self.sinks or kind not in HEALTH_KINDS:
            return False
        if kind != "watching":
            if now - self.last_health < self.health_cooldown:
                log.info("'%s' alert not sent: %.0f s since the last one (cooldown %.0f s)", kind,
                         now - self.last_health, self.health_cooldown)
                return False
            self.last_health = now
        alert = compose(kind, dict(info), base_url=self.base_url, now=now)
        return self._dispatch(alert, status, kind)

    def _dispatch(self, alert: dict, status: dict, what: str) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            log.error("alert for %s not sent: no event loop", what)
            return False
        for s in self.sinks:
            t = asyncio.create_task(self._send(s, alert, dict(status), self.attempts))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return True

    async def test(self, sink: Sink, status: dict | None = None) -> dict:
        """One immediate test message (no retries, so the real error shows at once)."""
        alert = compose("test", None, base_url=self.base_url)
        return await self._send(sink, alert, dict(status or {}), 1)

    async def _send(self, sink: Sink, alert: dict, status: dict, attempts: int) -> dict:
        ev = alert.get("event") or {}
        res = await deliver(sink, alert, status, attempts, self.backoff)
        rec = dict(res, t=time.time(), channel=sink.id, sink=sink.id, kind=alert["kind"], event=ev.get("id"))
        self.results[sink.id] = rec
        if rec["ok"]:
            self.last_ok[sink.id] = rec["t"]
            log.info("alert sent via %s (%s%s)", sink.id, alert["kind"],
                     f", event #{ev['id']}" if ev.get("id") is not None else "")
        else:
            log.warning("alert via %s failed after %d attempt(s): %s", sink.id, rec["attempts"], rec["error"])
        self.log = (self.log + [rec])[-50:]
        if self.on_result is not None:
            try:
                self.on_result(rec)
            except Exception:  # noqa: BLE001
                log.exception("alert result callback failed")
        return rec

    async def drain(self, timeout: float = 10) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    async def close(self) -> None:
        await self.drain()
        for s in self.sinks:
            await s.close()
