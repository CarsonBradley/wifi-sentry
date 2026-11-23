"""Access token for the HTTP API.

The dashboard is plain HTTP meant for a trusted network or a VPN such as Tailscale; it is not
built to face the internet. Every /api/ request needs the access token, including requests from this
Mac itself (other local programs, other macOS accounts and browser extensions can reach 127.0.0.1 too;
the CLI reads the token from its 0600 file):

  * `Authorization: Bearer <token>`, or the session cookie. The cookie is set by opening the dashboard
    once with `?token=<token>` (`./wifi-sentry open` does that on this Mac; the server then redirects
    to `/`) or by the dashboard's "enter access token" form (POST /api/login). It is HttpOnly,
    SameSite=Strict and holds a random session id bound to the token with an HMAC. Sessions expire
    server-side after `cookie_days`, logout revokes the session, and rotating the token (delete the
    file, restart) invalidates every session.
  * `[auth] loopback_exempt = true` (opt-in, unsafe on a Mac shared with other accounts or untrusted
    software) lets requests from this Mac skip the token: loopback peer, on a loopback listener, with a
    loopback Host header (stops DNS rebinding) and no proxy headers (stops a local reverse proxy such as
    `tailscale serve` from turning everyone's requests into "local" ones). Port forwards that don't add
    headers (ssh -L, socat, `tailscale serve --tcp`, Docker port maps) still make remote users look
    local, which is why it is off by default.
  * wrong tokens are counted per client address (never for loopback peers, so a local program can't
    lock the owner out); after `max_failures` within `failure_window_s` further wrong tokens from that
    address get 429. A correct token is always accepted: its 192 bits are what stops guessing, the
    counter only keeps the log quiet. Tokens are compared in constant time and never logged.

The static dashboard files are served without a token (they contain no data); every /api/ endpoint
except /api/auth, /api/login and /api/logout requires it."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from pathlib import Path

from .net import is_loopback_addr

log = logging.getLogger("sentry.auth")

PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-Port",
                 "X-Forwarded-Server", "X-Original-Forwarded-For", "X-Original-Host", "Forwarded", "X-Real-IP",
                 "X-Client-IP", "X-Cluster-Client-IP", "CF-Connecting-IP", "True-Client-IP", "Fastly-Client-IP",
                 "Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-User-Profile-Pic", "Via")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_TRACKED = 1024
TOKEN_MIN_LEN = 16
MAX_SESSIONS = 200


class RedactTokens(logging.Filter):
    """Scrub ?token=... from log records (aiohttp's access log is off, but if anyone turns it on)."""
    RE = re.compile(r"([?&]token=)[^\s&\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "token=" in record.msg:
            record.msg = self.RE.sub(r"\1<redacted>", record.msg)
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(self.RE.sub(r"\1<redacted>", a) if isinstance(a, str) else a for a in record.args)
        return True


for _name in ("aiohttp.access", "aiohttp.server", "aiohttp.web"):
    logging.getLogger(_name).addFilter(RedactTokens())


def load_or_create_token(path: str | Path) -> tuple[str, bool]:
    """Read the token file, creating it (mode 0600) with a fresh random token if missing or empty.
    Returns (token, created)."""
    p = Path(path)
    try:
        tok = p.read_text().strip()
    except FileNotFoundError:
        tok = ""
    if len(tok) >= TOKEN_MIN_LEN:
        return tok, False
    if tok:
        raise ValueError(f"{p}: the access token is shorter than {TOKEN_MIN_LEN} characters; delete the file "
                         "to have a new one generated")
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)  # 192 bits
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    os.replace(tmp, p)
    return tok, True


def host_without_port(host: str | None) -> str:
    h = (host or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h[1:]
    if h.count(":") == 1:
        return h.rsplit(":", 1)[0]
    return h


class Auth:
    def __init__(self, token: str, cookie_name: str = "wifi_sentry", loopback_exempt: bool = False,
                 max_failures: int = 10, failure_window_s: float = 300.0, cookie_days: float = 30.0,
                 sessions_path: str | Path | None = None):
        self.token = token.encode()
        self.cookie_name = cookie_name
        self.loopback_exempt = loopback_exempt
        self.max_failures = int(max_failures)
        self.window = float(failure_window_s)
        self.cookie_max_age = int(cookie_days * 86400)
        self.failures: dict[str, deque] = {}
        self.total_failures = 0
        self._last_log = 0.0
        # Sessions: sha256(session id) -> expiry (unix s). Persisted (mode 0600) so a restart doesn't log
        # every phone out; bound to this token by the HMAC in the cookie and by a token fingerprint in the file.
        self.sessions_path = Path(sessions_path) if sessions_path else None
        self._fp = hmac.new(self.token, b"wifi-sentry-sessions-v2", hashlib.sha256).hexdigest()[:16]
        self.sessions: dict[str, float] = {}
        self._load_sessions()

    # -- who is asking -------------------------------------------------------
    def is_local(self, request) -> bool:
        if not self.loopback_exempt:
            return False
        if not is_loopback_addr(request.remote):
            return False
        sock = request.transport.get_extra_info("sockname") if request.transport else None
        if sock and not is_loopback_addr(str(sock[0])):
            return False
        if host_without_port(request.host) not in LOOPBACK_HOSTS:
            return False
        return not any(h in request.headers for h in PROXY_HEADERS)

    # -- credentials ----------------------------------------------------------
    def token_ok(self, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(presented.strip().encode(), self.token)

    def _sig(self, sid: str) -> str:
        return hmac.new(self.token, b"wifi-sentry-session-v2:" + sid.encode(), hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def session_of(self, request) -> str | None:
        """The valid session id in the request's cookie, or None."""
        v = request.cookies.get(self.cookie_name) or ""
        sid, _, sig = v.partition(".")
        if not sid or not sig or not hmac.compare_digest(sig.encode(), self._sig(sid).encode()):
            return None
        exp = self.sessions.get(self._key(sid))
        if exp is None or exp < time.time():
            return None
        return sid

    def cookie_ok(self, request) -> bool:
        return self.session_of(request) is not None

    @staticmethod
    def bearer(request) -> str | None:
        h = request.headers.get("Authorization", "")
        if h[:7].lower() == "bearer ":
            return h[7:].strip()
        return None

    # -- rate limiting --------------------------------------------------------
    def _recent(self, ip: str, now: float) -> deque:
        q = self.failures.get(ip)
        if q is None:
            return deque()
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self.failures[ip]
        return q

    def blocked_for(self, ip: str, now: float | None = None) -> float:
        """Seconds this address must wait before another attempt (0 = may try)."""
        now = time.monotonic() if now is None else now
        q = self._recent(ip, now)
        if len(q) >= self.max_failures:
            return max(1.0, q[0] + self.window - now)
        return 0.0

    def record_failure(self, ip: str, how: str, now: float | None = None) -> None:
        if is_loopback_addr(ip):
            # A local program could otherwise lock the owner's own browser out; local brute force is
            # hopeless anyway (192-bit token).
            log.warning("wrong access token (%s) from this Mac", how)
            return
        now = time.monotonic() if now is None else now
        if ip not in self.failures and len(self.failures) >= MAX_TRACKED:
            # forget the address whose latest failure is oldest
            oldest = min(self.failures, key=lambda k: self.failures[k][-1] if self.failures[k] else 0)
            del self.failures[oldest]
        q = self.failures.setdefault(ip, deque())
        q.append(now)
        while len(q) > self.max_failures * 2:
            q.popleft()
        self.total_failures += 1
        if now - self._last_log > 10 or len(q) == self.max_failures:
            self._last_log = now
            log.warning("wrong access token (%s) from %s; %d in the last %.0f s%s", how, ip, len(q), self.window,
                        " - further attempts refused for a while" if len(q) >= self.max_failures else "")

    # -- cookies / sessions ------------------------------------------------------
    def _load_sessions(self) -> None:
        if self.sessions_path is None:
            return
        try:
            raw = json.loads(self.sessions_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("token") != self._fp:
            return  # another token (rotated): every old session is void
        now = time.time()
        self.sessions = {k: float(v) for k, v in (raw.get("sessions") or {}).items()
                         if isinstance(k, str) and isinstance(v, (int, float)) and v > now}

    def _save_sessions(self) -> None:
        if self.sessions_path is None:
            return
        try:
            self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.sessions_path.with_name(self.sessions_path.name + f".{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"token": self._fp, "sessions": self.sessions}, f)
            os.replace(tmp, self.sessions_path)
        except OSError as e:
            log.warning("could not save login sessions: %s", e)

    def set_cookie(self, resp) -> None:
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        while len(self.sessions) >= MAX_SESSIONS:
            del self.sessions[min(self.sessions, key=self.sessions.get)]
        sid = secrets.token_urlsafe(24)
        self.sessions[self._key(sid)] = now + self.cookie_max_age
        self._save_sessions()
        resp.set_cookie(self.cookie_name, f"{sid}.{self._sig(sid)}", max_age=self.cookie_max_age, path="/",
                        httponly=True, samesite="Strict")

    def logout(self, request, resp) -> None:
        sid = self.session_of(request)
        if sid is not None:
            self.sessions.pop(self._key(sid), None)
            self._save_sessions()
        self.clear_cookie(resp)

    def clear_cookie(self, resp) -> None:
        resp.del_cookie(self.cookie_name, path="/")
