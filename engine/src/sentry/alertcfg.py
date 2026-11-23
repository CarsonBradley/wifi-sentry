"""Alert channel settings made in the dashboard.

Stored in <data_dir>/alerts.json (mode 0600; data/ is git-ignored), never in a committed file.
Per channel, a dashboard entry replaces the [alerts.<channel>] section of config.toml /
config.local.toml; channels never touched in the dashboard keep using the config file, so existing
hand-written setups go on working. "Reset" removes the dashboard entry again.

Secrets (ntfy token, webhook URL, SMTP password) are write-only through the API: `public()` reports
only whether they are set (plus the webhook's scheme and host, so you can tell which one it is).

File format:
  {"version": 1,
   "settings": {"cooldown_s": 300, "notify_on_close": false, "base_url": ""},   # keys optional
   "channels": {"ntfy": {...}, "webhook": {...}, "email": {...}}}                 # entries optional
"""
from __future__ import annotations

import base64
import copy
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

from . import sinks
from .sinks import EmailSink, NtfySink, Sink, WebhookSink

log = logging.getLogger("sentry.alertcfg")

CHANNELS = ("ntfy", "webhook", "email")
LABELS = {"ntfy": "Phone (ntfy)", "webhook": "Webhook", "email": "Email"}
SECRETS = {"ntfy": ("token",), "webhook": ("url",), "email": ("password",)}
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
NTFY_PRIORITIES = ("min", "low", "default", "high", "urgent")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_COOLDOWN_S = 86400


def new_topic() -> str:
    """A long random ntfy topic (120 bits): anyone who knows a topic can read it, so it has to be unguessable."""
    return "sentry-" + base64.b32encode(secrets.token_bytes(15)).decode().lower()


def _blank(kind: str) -> dict:
    return {
        "ntfy": {"enabled": False, "server": DEFAULT_NTFY_SERVER, "topic": "", "token": "", "priority": "high"},
        "webhook": {"enabled": False, "url": "", "format": "json"},
        "email": {"enabled": False, "host": "", "port": 587, "security": "starttls", "username": "",
                  "password": "", "from": "", "to": []},
    }[kind]


def _http_url(v, what: str, allow_empty: bool = False) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{what} must be a string")
    v = v.strip()
    if not v and allow_empty:
        return ""
    if any(c in v for c in "\r\n\t ") or len(v) > 2000:
        raise ValueError(f"{what} is not a valid URL")
    try:
        u = urlsplit(v)
    except ValueError:
        raise ValueError(f"{what} is not a valid URL") from None
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"{what} must start with http:// or https:// and name a host")
    try:
        u.port  # raises for a port that isn't 0..65535
    except ValueError:
        raise ValueError(f"{what} has an invalid port") from None
    if u.username is not None or u.password is not None:
        raise ValueError(f"{what} must not contain a user name or password (user:password@); "
                         "for ntfy put them in the access token field as user:password")
    h = u.hostname.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
    except ValueError:
        ip = None
    loopback = h == "localhost" or h.endswith(".localhost") or (ip is not None and ip.is_loopback)
    if (loopback and not sinks.ALLOW_LOOPBACK_TARGETS) or (
            ip is not None and (ip.is_link_local or ip.is_unspecified or ip.is_multicast)):
        raise ValueError(f"{what} can't point at this Mac itself or a link-local address "
                         "(a server elsewhere on your network is fine)")
    return v


def _text(v, what: str, max_len: int = 200) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(f"{what} must be a string")
    v = v.strip()
    if "\r" in v or "\n" in v or len(v) > max_len:
        raise ValueError(f"{what} must be a single line of at most {max_len} characters")
    return v


def _address(v: str, what: str) -> str:
    v = _text(v, what, 254)
    if v and (v.count("@") != 1 or v.startswith("@") or v.endswith("@") or " " in v or "," in v):
        raise ValueError(f"{what}: {v!r} doesn't look like an email address")
    return v


def split_ntfy_url(url: str) -> tuple[str, str]:
    """'https://ntfy.sh/topic' -> ('https://ntfy.sh', 'topic'); a server under a sub-path keeps it."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return "", ""
    server, _, topic = url.rpartition("/")
    if "://" not in server:  # just "https://host" without a topic
        return url, ""
    return server, topic


class AlertConfig:
    def __init__(self, path: str | Path | None, cfg: dict):
        self.path = Path(path) if path else None
        self.cfg = cfg
        self.data: dict = {"version": 1, "settings": {}, "channels": {}}
        if self.path is not None:
            self.load()

    # ------------------------------------------------------------------ file
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("%s is unreadable (%s); dashboard alert settings ignored, config file used", self.path, e)
            return
        try:
            if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass
        if isinstance(raw, dict):
            self.data["settings"] = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
            ch = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
            self.data["channels"] = {k: v for k, v in ch.items() if k in CHANNELS and isinstance(v, dict)}

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ config-file channels
    def file_channel(self, kind: str) -> dict | None:
        """The [alerts.<kind>] section translated to the dashboard's shape; None if it names no target."""
        a = self.cfg.get("alerts", {}).get(kind) or {}
        ch = _blank(kind)
        if kind == "ntfy":
            server, topic = split_ntfy_url(str(a.get("url") or ""))
            if not topic:
                return None
            ch.update(enabled=bool(a.get("enabled")), server=server, topic=topic, token=str(a.get("token") or ""),
                      priority=str(a.get("priority") or "high"))
        elif kind == "webhook":
            if not a.get("url"):
                return None
            ch.update(enabled=bool(a.get("enabled")), url=str(a["url"]), format=str(a.get("format") or "json"))
        else:
            if not a.get("host"):
                return None
            port = int(a.get("port") or 587)
            sec = a.get("security")
            if sec not in EmailSink.SECURITY:
                sec = "starttls" if a.get("starttls", True) else ("ssl" if port == 465 else "none")
            to = a.get("to") or []
            ch.update(enabled=bool(a.get("enabled")), host=str(a["host"]), port=port, security=sec,
                      username=str(a.get("username") or ""), password=str(a.get("password") or ""),
                      **{"from": str(a.get("from") or ""), "to": [to] if isinstance(to, str) else list(to)})
        return ch

    def channel(self, kind: str) -> tuple[dict | None, str]:
        """(settings, source) with source 'dashboard', 'config_file' or 'none'."""
        if kind in self.data["channels"]:
            ch = _blank(kind)
            ch.update(self.data["channels"][kind])
            return ch, "dashboard"
        ch = self.file_channel(kind)
        return (ch, "config_file") if ch is not None else (None, "none")

    @staticmethod
    def missing(kind: str, ch: dict) -> list[str]:
        if kind == "ntfy":
            return [] if ch.get("topic") and ch.get("server") else ["topic"]
        if kind == "webhook":
            return [] if ch.get("url") else ["url"]
        return [f for f in ("host", "from", "to") if not ch.get(f)]

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        a = self.cfg.get("alerts", {})
        s = {"cooldown_s": float(a.get("cooldown_s", 300)), "notify_on_close": bool(a.get("notify_on_close", False)),
             "base_url": str(a.get("base_url") or "")}
        s.update({k: v for k, v in self.data["settings"].items() if k in s})
        return s

    def update_settings(self, body: dict) -> dict:
        new = dict(self.data["settings"])
        for k, v in body.items():
            if k == "cooldown_s":
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= MAX_COOLDOWN_S:
                    raise ValueError(f"cooldown_s must be a number of seconds between 0 and {MAX_COOLDOWN_S}")
                new[k] = float(v)
            elif k == "notify_on_close":
                if not isinstance(v, bool):
                    raise ValueError("notify_on_close must be a boolean")
                new[k] = v
            elif k == "base_url":
                new[k] = _http_url(v, "dashboard link", allow_empty=True)
            else:
                raise ValueError(f"unknown setting {k!r}")
        self.data["settings"] = new
        self.save()
        return self.settings()

    # ------------------------------------------------------------------ channel edits
    def update_channel(self, kind: str, body: dict) -> None:
        """Merge `body` into the channel. Omitted keys keep their value; for secrets, null or an omitted
        key keeps the stored secret and "" clears it."""
        if kind not in CHANNELS:
            raise KeyError(kind)
        cur, _ = self.channel(kind)
        ch = copy.deepcopy(cur) if cur is not None else _blank(kind)
        for k, v in body.items():
            if k in SECRETS[kind] and v is None:
                continue
            if k == "enabled":
                if not isinstance(v, bool):
                    raise ValueError("enabled must be a boolean")
                ch[k] = v
            elif kind == "ntfy" and k == "server":
                ch[k] = _http_url(v, "ntfy server").rstrip("/")
            elif kind == "ntfy" and k == "topic":
                v = _text(v, "topic", 64)
                if v and not TOPIC_RE.match(v):
                    raise ValueError("the topic may only use letters, digits, - and _ (up to 64)")
                ch[k] = v
            elif kind == "ntfy" and k == "token":
                ch[k] = _text(v, "access token", 300)
            elif kind == "ntfy" and k == "priority":
                if v not in NTFY_PRIORITIES:
                    raise ValueError("priority must be one of " + ", ".join(NTFY_PRIORITIES))
                ch[k] = v
            elif kind == "webhook" and k == "url":
                ch[k] = _http_url(v, "webhook URL", allow_empty=True)
            elif kind == "webhook" and k == "format":
                if v not in WebhookSink.FORMATS:
                    raise ValueError("format must be one of " + ", ".join(WebhookSink.FORMATS))
                ch[k] = v
            elif kind == "email" and k in ("host", "username"):
                ch[k] = _text(v, k, 254)
            elif kind == "email" and k == "password":
                ch[k] = _text(v, "password", 500)
            elif kind == "email" and k == "port":
                if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
                    raise ValueError("port must be a number from 1 to 65535")
                ch[k] = v
            elif kind == "email" and k == "security":
                if v not in EmailSink.SECURITY:
                    raise ValueError("security must be one of " + ", ".join(EmailSink.SECURITY))
                ch[k] = v
            elif kind == "email" and k == "from":
                ch[k] = _address(v, "from")
            elif kind == "email" and k == "to":
                items = v.split(",") if isinstance(v, str) else v
                if not isinstance(items, list) or len(items) > 10:
                    raise ValueError("to must be up to 10 addresses")
                ch[k] = [a for a in (_address(x, "to") for x in items) if a]
            else:
                raise ValueError(f"unknown {kind} setting {k!r}")
        if ch.get("enabled"):
            miss = self.missing(kind, ch)
            if miss:
                raise ValueError(f"can't turn {LABELS[kind]} on yet: {', '.join(miss)} missing")
        self.data["channels"][kind] = ch
        self.save()

    def reset_channel(self, kind: str) -> None:
        if kind not in CHANNELS:
            raise KeyError(kind)
        self.data["channels"].pop(kind, None)
        self.save()

    # ------------------------------------------------------------------ sinks
    def sink(self, kind: str, require_enabled: bool = True) -> Sink | None:
        ch, _ = self.channel(kind)
        if ch is None or self.missing(kind, ch) or (require_enabled and not ch.get("enabled")):
            return None
        if kind == "ntfy":
            return NtfySink(ch["server"].rstrip("/") + "/" + ch["topic"], ch.get("priority") or "high", ch.get("token", ""))
        if kind == "webhook":
            return WebhookSink(ch["url"], ch.get("format") or "json")
        return EmailSink(ch["host"], ch["port"], ch["security"], ch["username"], ch["password"], ch["from"], ch["to"])

    def build_sinks(self) -> list[Sink]:
        out = []
        for k in CHANNELS:
            try:
                s = self.sink(k)
            except (KeyError, TypeError, ValueError) as e:  # a hand-edited file with a bad value
                log.error("alert channel %s is misconfigured (%r); it is off", k, e)
                continue
            if s is not None:
                out.append(s)
        return out

    # ------------------------------------------------------------------ API view
    def public(self, kind: str) -> dict:
        ch, source = self.channel(kind)
        out = {"id": kind, "label": LABELS[kind], "source": source, "configured": ch is not None,
               "enabled": bool(ch and ch.get("enabled")), "complete": bool(ch) and not self.missing(kind, ch)}
        view = copy.deepcopy(ch) if ch is not None else _blank(kind)
        secrets_set = {}
        for k in SECRETS[kind]:
            secrets_set[k] = bool(view.pop(k, ""))
        out["secrets"] = secrets_set
        if kind == "webhook" and ch and ch.get("url"):
            try:
                u = urlsplit(ch["url"])
                out["url_hint"] = f"{u.scheme}://{u.hostname}{(':' + str(u.port)) if u.port else ''}/…"
            except ValueError:
                out["url_hint"] = "set"
        out["settings"] = view
        return out
