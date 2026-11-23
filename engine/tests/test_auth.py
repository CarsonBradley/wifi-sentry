"""Access control: every API request needs the token (Bearer or the session cookie), also from this Mac,
unless [auth] loopback_exempt = true is opted into.

Remote clients are simulated on 127.0.0.1: a request whose Host header isn't a loopback name, or that
carries proxy headers, is treated exactly like one arriving over Tailscale / the LAN (no network needed)."""
import asyncio
import logging
import os
import stat
import types

import pytest
from aiohttp import DummyCookieJar
from aiohttp.test_utils import TestClient, TestServer

from sentry.app import create_app
from sentry.auth import Auth, host_without_port, load_or_create_token

from test_api import make_engine

REMOTE = {"Host": "100.64.10.20:8450"}  # what a phone on the tailnet sends


async def _client(eng):
    c = TestClient(TestServer(create_app(eng)), cookie_jar=DummyCookieJar())  # cookies only where a test sends them
    await c.start_server()
    return c


def with_client(tmp_path, fn, **extra):
    async def go():
        eng = make_engine(tmp_path, **extra)
        c = await _client(eng)
        try:
            await fn(eng, c)
        finally:
            await c.close()
            await eng.stop()
    asyncio.run(go())


def token_of(eng) -> str:
    return eng.token_path.read_text().strip()


# ---------------------------------------------------------------- token file
def test_token_file_created_private_and_reused(tmp_path):
    p = tmp_path / "d" / "access-token"
    tok, created = load_or_create_token(p)
    assert created and len(tok) >= 30
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert load_or_create_token(p) == (tok, False)
    p.write_text("short\n")
    with pytest.raises(ValueError):
        load_or_create_token(p)


def test_engine_creates_token_at_first_start(tmp_path):
    eng = make_engine(tmp_path)
    assert eng.token_path == tmp_path / "data" / "access-token" and eng.token_path.exists()
    tok = token_of(eng)
    eng.storage.close()
    eng2 = make_engine(tmp_path)  # restart keeps it
    assert token_of(eng2) == tok
    eng2.storage.close()


# ---------------------------------------------------------------- loopback vs remote
def test_loopback_needs_the_token_by_default(tmp_path):
    """Other programs, accounts and extensions on the Mac reach 127.0.0.1 too (security review M1)."""
    async def fn(eng, c):
        for path in ("/api/status", "/api/events", "/api/debug", "/api/stream", "/api/alerts"):
            assert (await c.get(path)).status == 401, path
        for path, body in (("/api/arm", {"armed": False}), ("/api/walktest", {"action": "start"}),
                           ("/api/relearn", None), ("/api/alerts/channels/webhook", {"url": "http://192.0.2.9/x"})):
            assert (await c.post(path, json=body)).status == 401, path
        assert eng.detector.walk is None
        a = await (await c.get("/api/auth")).json()
        assert a == {"local": False, "required": True, "authenticated": False}
        h = {"Authorization": "Bearer " + token_of(eng)}
        assert (await c.get("/api/status", headers=h)).status == 200
        assert (await (await c.get("/api/auth", headers=h)).json())["authenticated"] is True
    with_client(tmp_path, fn)


def test_loopback_exempt_is_an_opt_in(tmp_path):
    async def fn(eng, c):
        assert (await c.get("/api/status")).status == 200
        a = await (await c.get("/api/auth")).json()
        assert a == {"local": True, "required": False, "authenticated": True}
        assert (await c.post("/api/arm", json={"armed": True}, headers={"Host": "localhost:8450"})).status == 200
    with_client(tmp_path, fn, auth={"loopback_exempt": True})


def test_remote_requires_token_everywhere_on_the_api(tmp_path):
    async def fn(eng, c):
        for path in ("/api/status", "/api/events", "/api/history", "/api/gaps", "/api/debug", "/api/walktest",
                     "/api/stream", "/api/nope"):
            r = await c.get(path, headers=REMOTE)
            assert r.status == 401, path
            body = await r.json()
            assert body["auth"] == "required" and "token" in body["error"]
            assert r.headers["WWW-Authenticate"].startswith("Bearer")
        r = await c.post("/api/arm", json={"armed": True}, headers=REMOTE)
        assert r.status == 401 and eng.detector.armed is False
        a = await (await c.get("/api/auth", headers=REMOTE)).json()
        assert a == {"local": False, "required": True, "authenticated": False}
        # the dashboard files themselves carry no data and load without a token (to show the login)
        r = await c.get("/", headers=REMOTE)
        assert r.status == 200
    with_client(tmp_path, fn)


@pytest.mark.parametrize("hdr", [{"X-Forwarded-For": "100.64.1.1"}, {"Forwarded": "for=100.64.1.1"},
                                 {"Tailscale-User-Login": "someone@example.com"}, {"Host": "evil.example:8450"},
                                 {"CF-Connecting-IP": "203.0.113.9"}, {"True-Client-IP": "203.0.113.9"},
                                 {"X-Client-IP": "203.0.113.9"}, {"X-Forwarded-Port": "443"},
                                 {"X-Original-Forwarded-For": "203.0.113.9"}])
def test_proxied_or_rebound_loopback_requests_are_not_local(tmp_path, hdr):
    """Even with the opt-in exemption: `tailscale serve` / other proxies (headers) and DNS rebinding
    (foreign Host) both arrive from 127.0.0.1."""
    async def fn(eng, c):
        assert (await c.get("/api/status", headers=hdr)).status == 401
    with_client(tmp_path, fn, auth={"loopback_exempt": True})


def test_is_local_checks_peer_listener_host_and_headers():
    assert not Auth("x" * 32).is_local(types.SimpleNamespace(remote="127.0.0.1"))   # off by default
    a = Auth("x" * 32, loopback_exempt=True)

    def req(remote="127.0.0.1", sock=("127.0.0.1", 8450), host="127.0.0.1:8450", headers=None):
        return types.SimpleNamespace(remote=remote, host=host, headers=headers or {},
                                     transport=types.SimpleNamespace(get_extra_info=lambda k: sock))
    assert a.is_local(req())
    assert a.is_local(req(remote="::1", sock=("::1", 8450, 0, 0), host="[::1]:8450"))
    assert a.is_local(req(remote="::ffff:127.0.0.1", host="localhost"))
    assert not a.is_local(req(remote="100.64.0.7"))                       # tailnet peer
    assert not a.is_local(req(remote="192.168.1.20"))                     # LAN peer
    assert not a.is_local(req(sock=("100.64.0.1", 8450)))                 # arrived on the exposed listener
    assert not a.is_local(req(host="wifi-sentry.example:8450"))
    assert not a.is_local(req(headers={"X-Real-IP": "1.2.3.4"}))
    assert host_without_port("[::1]:8450") == "::1" and host_without_port("LOCALHOST") == "localhost"


# ---------------------------------------------------------------- bearer
def test_bearer_token(tmp_path):
    async def fn(eng, c):
        tok = token_of(eng)
        r = await c.get("/api/status", headers=dict(REMOTE, Authorization="Bearer " + tok))
        assert r.status == 200 and (await r.json())["state"] == "offline"
        r = await c.get("/api/status", headers=dict(REMOTE, Authorization="bearer " + tok))
        assert r.status == 200
        r = await c.get("/api/status", headers=dict(REMOTE, Authorization="Bearer " + tok[:-1] + "x"))
        assert r.status == 401 and (await r.json())["error"] == "wrong access token"
        r = await c.post("/api/arm", json={"armed": True}, headers=dict(REMOTE, Authorization="Bearer " + tok))
        assert r.status == 200 and eng.detector.armed is True
    with_client(tmp_path, fn)


# ---------------------------------------------------------------- browser: link + cookie
def test_token_link_sets_cookie_and_strips_the_token(tmp_path):
    async def fn(eng, c):
        tok = token_of(eng)
        r = await c.get(f"/?token={tok}&x=1", headers=REMOTE, allow_redirects=False)
        assert r.status == 303
        assert r.headers["Location"] == "/"
        sc = r.headers["Set-Cookie"]
        assert "HttpOnly" in sc and "SameSite=Strict" in sc and "Path=/" in sc
        assert tok not in sc                                    # a random session id, not the token
        cookie = sc.split(";", 1)[0]
        assert cookie.startswith(eng.auth.cookie_name + "=")
        h = dict(REMOTE, Cookie=cookie)
        assert (await c.get("/api/status", headers=h)).status == 200
        assert (await (await c.get("/api/auth", headers=h)).json())["authenticated"] is True
        r = await c.get("/api/stream", headers=h)              # SSE with the cookie
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
        r.close()
        assert (await c.post("/api/sensitivity", json={"level": "high"}, headers=h)).status == 200
        # a stale/forged cookie is refused and cleared
        r = await c.get("/api/status", headers=dict(REMOTE, Cookie=eng.auth.cookie_name + "=deadbeef"))
        assert r.status == 401 and eng.auth.cookie_name + '=""' in r.headers.get("Set-Cookie", "")
    with_client(tmp_path, fn)


def test_wrong_token_link_redirects_to_the_login(tmp_path):
    async def fn(eng, c):
        r = await c.get("/?token=not-the-token", headers=REMOTE, allow_redirects=False)
        assert r.status == 303 and r.headers["Location"] == "/#token-rejected"
        assert "Set-Cookie" not in r.headers
        assert eng.auth.total_failures == 0          # from this Mac: not counted (see below)
    with_client(tmp_path, fn)


def test_login_and_logout(tmp_path):
    async def fn(eng, c):
        r = await c.post("/api/login", json={"token": "nope"}, headers=REMOTE)
        assert r.status == 401
        r = await c.post("/api/login", json={"token": token_of(eng)}, headers=REMOTE)
        assert r.status == 200 and "SameSite=Strict" in r.headers["Set-Cookie"]
        cookie = r.headers["Set-Cookie"].split(";", 1)[0]
        assert (await c.get("/api/events", headers=dict(REMOTE, Cookie=cookie))).status == 200
        r = await c.post("/api/logout", headers=dict(REMOTE, Cookie=cookie))
        assert r.status == 200 and "Max-Age=0" in r.headers["Set-Cookie"]
        # logout revokes the session on the server: the old cookie value no longer works
        assert (await c.get("/api/events", headers=dict(REMOTE, Cookie=cookie))).status == 401
        # a login from another site is refused like every cross-origin POST
        r = await c.post("/api/login", json={"token": token_of(eng)},
                         headers=dict(REMOTE, Origin="http://evil.example"))
        assert r.status == 403
    with_client(tmp_path, fn)


# ---------------------------------------------------------------- rate limit
def test_failed_attempts_are_rate_limited_but_never_lock_out_the_right_token(tmp_path, monkeypatch):
    import sentry.auth
    monkeypatch.setattr(sentry.auth, "is_loopback_addr", lambda ip: False)  # the test client is "remote"

    async def fn(eng, c):
        tok = token_of(eng)
        for i in range(10):
            r = await c.post("/api/login", json={"token": f"guess-{i}"}, headers=REMOTE)
            assert r.status == 401
        r = await c.post("/api/login", json={"token": "guess-11"}, headers=REMOTE)
        assert r.status == 429 and int(r.headers["Retry-After"]) > 0
        r = await c.get("/api/status", headers=dict(REMOTE, Authorization="Bearer wrong"))
        assert r.status == 429
        r = await c.get("/?token=wrong", headers=REMOTE, allow_redirects=False)
        assert r.headers["Location"].endswith("#too-many-attempts")
        # the correct token always works (security review L2): guessing is stopped by its 192 bits
        assert (await c.post("/api/login", json={"token": tok}, headers=REMOTE)).status == 200
        assert (await c.get("/api/status", headers=dict(REMOTE, Authorization="Bearer " + tok))).status == 200
        r = await c.get(f"/?token={tok}", headers=REMOTE, allow_redirects=False)
        assert r.headers["Location"] == "/" and "Set-Cookie" in r.headers
    with_client(tmp_path, fn)


def test_loopback_peers_are_never_counted(tmp_path):
    """A local program spamming wrong tokens must not lock the owner's own browser out."""
    async def fn(eng, c):
        for i in range(30):
            await c.post("/api/login", json={"token": f"guess-{i}"})
        assert eng.auth.failures == {} and eng.auth.blocked_for("127.0.0.1") == 0.0
    with_client(tmp_path, fn)


@pytest.mark.parametrize("path", ["/\\evil.example/", "/%2F%2Fevil.example/", "/a/b?x=1"])
def test_token_link_only_redirects_to_the_dashboard(tmp_path, path):
    """No open redirect (security review L3): the target is always "/" on this origin."""
    async def fn(eng, c):
        for tok in (token_of(eng), "wrong"):
            sep = "&" if "?" in path else "?"
            r = await c.get(path + sep + "token=" + tok, headers=REMOTE, allow_redirects=False)
            assert r.status == 303 and r.headers["Location"] in ("/", "/#token-rejected"), r.headers["Location"]
    with_client(tmp_path, fn)


def test_scheme_relative_token_link_is_not_an_open_redirect(tmp_path):
    from aiohttp.test_utils import make_mocked_request
    from sentry.app import _check_access
    eng = make_engine(tmp_path)
    try:
        for tok in (token_of(eng), "wrong"):
            req = make_mocked_request("GET", "//evil.example/?token=" + tok, headers=REMOTE)
            r = _check_access(eng, req)
            assert r.status == 303 and r.headers["Location"] in ("/", "/#token-rejected")
    finally:
        eng.storage.close()


def test_sessions_expire_on_the_server_and_die_with_the_token(tmp_path):
    async def fn(eng, c):
        r = await c.post("/api/login", json={"token": token_of(eng)}, headers=REMOTE)
        cookie = r.headers["Set-Cookie"].split(";", 1)[0]
        h = dict(REMOTE, Cookie=cookie)
        assert (await c.get("/api/status", headers=h)).status == 200
        assert "Max-Age=%d" % (30 * 86400) in r.headers["Set-Cookie"]      # 30 days by default
        # persisted: a restarted engine (same token) still knows the session
        a2 = Auth(token_of(eng), cookie_name=eng.auth.cookie_name, sessions_path=eng.data_dir / "sessions.json")
        assert len(a2.sessions) == 1
        assert stat.S_IMODE(os.stat(eng.data_dir / "sessions.json").st_mode) == 0o600
        # a rotated token voids every stored session
        assert Auth("z" * 32, sessions_path=eng.data_dir / "sessions.json").sessions == {}
        # server-side expiry, whatever the browser keeps
        for k in eng.auth.sessions:
            eng.auth.sessions[k] = 1.0
        assert (await c.get("/api/status", headers=h)).status == 401
    with_client(tmp_path, fn)


def test_rate_limiter_memory_is_bounded():
    a = Auth("y" * 32, max_failures=3, failure_window_s=60)
    for i in range(3000):
        a.record_failure(f"198.18.{i // 250}.{i % 250}", "test", now=float(i))
    assert len(a.failures) <= 1024
    assert a.blocked_for("198.18.0.1", now=10.0) == 0.0


def test_tokens_never_reach_the_log(tmp_path, caplog):
    async def fn(eng, c):
        tok = token_of(eng)
        with caplog.at_level(logging.DEBUG):
            await c.get(f"/?token={tok}", headers=REMOTE, allow_redirects=False)
            await c.get("/?token=WRONG-secret-guess", headers=REMOTE, allow_redirects=False)
            await c.post("/api/login", json={"token": "ANOTHER-wrong-one"}, headers=REMOTE)
            await c.get("/api/status", headers=dict(REMOTE, Authorization="Bearer THIRD-wrong"))
        text = caplog.text
        assert "wrong access token" in text
        for secret in (tok, "WRONG-secret-guess", "ANOTHER-wrong-one", "THIRD-wrong"):
            assert secret not in text
    with_client(tmp_path, fn)


def test_security_headers(tmp_path):
    async def fn(eng, c):
        for path in ("/", "/api/status"):
            r = await c.get(path)
            assert r.headers["X-Frame-Options"] == "DENY"
            assert r.headers["Referrer-Policy"] == "no-referrer"
            assert r.headers["X-Content-Type-Options"] == "nosniff"
        r = await c.get("/api/status", headers=REMOTE)
        assert r.status == 401 and r.headers["X-Frame-Options"] == "DENY"
    with_client(tmp_path, fn)
