"""Alert channels and first-run onboarding through the HTTP API. Every target is a local stub on
127.0.0.1; nothing reaches the real network."""
import asyncio
import json
import os
import stat
import time

from aiohttp.test_utils import TestClient, TestServer

from sentry.alertcfg import AlertConfig, new_topic, split_ntfy_url
from sentry.app import create_app

from conftest import cfg_with
from test_api import feed, make_engine, scenario
from test_sinks import FAST, http_stub, smtp_stub

REMOTE = {"Host": "100.64.10.20:8450", "Authorization": ""}  # a phone without the token


async def client(eng):
    c = TestClient(TestServer(create_app(eng)), headers={"Authorization": "Bearer " + eng.token_path.read_text().strip()})
    await c.start_server()
    return c


def run_with(tmp_path, fn, **extra):
    async def go():
        eng = make_engine(tmp_path, **extra)
        eng.router.backoff = FAST
        c = await client(eng)
        try:
            await fn(eng, c)
        finally:
            await c.close()
            await eng.stop()
    asyncio.run(go())


def channel(view, kind):
    return next(ch for ch in view["channels"] if ch["id"] == kind)


# ---------------------------------------------------------------- CRUD + redaction
def test_channel_crud_and_secret_redaction(tmp_path):
    async def fn(eng, c):
        v = await (await c.get("/api/alerts")).json()
        assert [ch["id"] for ch in v["channels"]] == ["ntfy", "webhook", "email"]
        assert all(ch["source"] == "none" and not ch["enabled"] for ch in v["channels"])
        assert v["settings"] == {"cooldown_s": 300.0, "notify_on_close": False, "base_url": ""}

        topic = (await (await c.post("/api/alerts/ntfy-topic")).json())["topic"]
        assert topic.startswith("sentry-") and len(topic) == 31 and topic != new_topic()
        r = await c.post("/api/alerts/channels/ntfy", json={"topic": topic, "token": "tk_secret_123", "enabled": True})
        assert r.status == 200
        text = await r.text()
        assert "tk_secret_123" not in text
        ch = channel(json.loads(text), "ntfy")
        assert ch["source"] == "dashboard" and ch["enabled"] and ch["secrets"] == {"token": True}
        assert ch["settings"]["topic"] == topic and ch["settings"]["server"] == "https://ntfy.sh"
        assert "token" not in ch["settings"]
        assert json.loads(text)["summary"]["channels"] == ["ntfy"]

        # stored only in data/alerts.json, mode 600
        f = eng.data_dir / "alerts.json"
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
        assert json.loads(f.read_text())["channels"]["ntfy"]["token"] == "tk_secret_123"

        # null / omitted keeps a secret, "" clears it
        await c.post("/api/alerts/channels/ntfy", json={"token": None, "priority": "urgent"})
        assert json.loads(f.read_text())["channels"]["ntfy"]["token"] == "tk_secret_123"
        v = await (await c.post("/api/alerts/channels/ntfy", json={"token": ""})).json()
        assert channel(v, "ntfy")["secrets"] == {"token": False} and channel(v, "ntfy")["settings"]["priority"] == "urgent"

        # webhook URL is a secret: only scheme + host come back
        v = await (await c.post("/api/alerts/channels/webhook", json={
            "url": "https://hooks.example.com:8443/services/T000/B000/XXXXSECRET", "format": "slack"})).json()
        w = channel(v, "webhook")
        assert w["url_hint"] == "https://hooks.example.com:8443/…" and "XXXXSECRET" not in json.dumps(v)
        assert w["settings"] == {"enabled": False, "format": "slack"}

        # email: password write-only, "to" accepts a comma list
        v = await (await c.post("/api/alerts/channels/email", json={
            "host": "smtp.example.com", "port": 465, "security": "ssl", "username": "me", "password": "pw!",
            "from": "me@example.com", "to": "a@example.com, b@example.com", "enabled": True})).json()
        e = channel(v, "email")
        assert e["secrets"] == {"password": True} and e["settings"]["to"] == ["a@example.com", "b@example.com"]
        assert "pw!" not in json.dumps(v) and v["summary"]["channels"] == ["ntfy", "email"]

        # validation: real error messages, nothing saved
        for kind, body, needle in (
                ("ntfy", {"topic": "has space"}, "topic"),
                ("ntfy", {"server": "ftp://x"}, "http"),
                ("webhook", {"enabled": True, "url": ""}, "url missing"),
                ("email", {"to": "not-an-address"}, "email address"),
                ("email", {"port": 0}, "port"),
                ("email", {"host": "a\r\nRCPT TO:<x@y>"}, "single line"),
                ("email", {"bogus": 1}, "unknown")):
            r = await c.post(f"/api/alerts/channels/{kind}", json=body)
            assert r.status == 400 and needle in (await r.json())["error"], (kind, body)
        assert (await c.post("/api/alerts/channels/sms", json={})).status == 404

        # settings
        v = await (await c.post("/api/alerts/settings", json={"cooldown_s": 120, "notify_on_close": True,
                                                                "base_url": "http://192.0.2.5:8450/"})).json()
        assert v["settings"] == {"cooldown_s": 120.0, "notify_on_close": True, "base_url": "http://192.0.2.5:8450/"}
        assert eng.router.cooldown == 120 and eng.router.notify_on_close and eng.router.base_url
        for bad in ({"cooldown_s": -1}, {"base_url": "javascript:alert(1)"}, {"x": 1}):
            assert (await c.post("/api/alerts/settings", json=bad)).status == 400

        # delete = back to the config file (here: nothing)
        v = await (await c.delete("/api/alerts/channels/email")).json()
        assert channel(v, "email")["source"] == "none" and v["summary"]["channels"] == ["ntfy"]

        # remote clients need the token; other sites can't post
        assert (await c.post("/api/alerts/channels/ntfy", json={"enabled": False}, headers=REMOTE)).status == 401
        r = await c.post("/api/alerts/channels/ntfy", json={"enabled": False}, headers={"Origin": "http://evil.example"})
        assert r.status == 403
        r = await c.delete("/api/alerts/channels/ntfy", headers={"Origin": "http://evil.example"})
        assert r.status == 403
    run_with(tmp_path, fn)


def test_config_file_channels_are_the_fallback(tmp_path):
    over = {"alerts": {"ntfy": {"enabled": True, "url": "http://127.0.0.1:9/old-topic", "token": "tk_file"},
                       "email": {"enabled": False, "host": "smtp.example.com", "port": 465, "starttls": False,
                                 "from": "a@example.com", "to": ["b@example.com"]}}}

    async def fn(eng, c):
        v = await (await c.get("/api/alerts")).json()
        n = channel(v, "ntfy")
        assert n["source"] == "config_file" and n["enabled"] and n["settings"]["topic"] == "old-topic"
        assert n["secrets"] == {"token": True} and "tk_file" not in json.dumps(v)
        assert channel(v, "email")["settings"]["security"] == "ssl"
        assert [s.id for s in eng.router.sinks] == ["ntfy"]
        # editing in the dashboard takes the channel over (keeping the file's token) ...
        v = await (await c.post("/api/alerts/channels/ntfy", json={"enabled": False})).json()
        assert channel(v, "ntfy")["source"] == "dashboard" and channel(v, "ntfy")["secrets"]["token"]
        assert eng.router.sinks == []
        # ... and reset hands it back
        v = await (await c.delete("/api/alerts/channels/ntfy")).json()
        assert channel(v, "ntfy")["source"] == "config_file" and [s.id for s in eng.router.sinks] == ["ntfy"]
    run_with(tmp_path, fn, alerts=over["alerts"])


def test_alert_file_permissions_are_repaired_and_bad_files_ignored(tmp_path):
    f = tmp_path / "alerts.json"
    f.write_text(json.dumps({"channels": {"ntfy": {"enabled": True, "server": "https://x.example", "topic": "t",
                                                   "token": "s"}}}))
    os.chmod(f, 0o644)
    ac = AlertConfig(f, cfg_with())
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600 and [s.id for s in ac.build_sinks()] == ["ntfy"]
    f.write_text("{not json")
    assert AlertConfig(f, cfg_with()).build_sinks() == []
    assert split_ntfy_url("https://ntfy.sh/abc") == ("https://ntfy.sh", "abc")
    assert split_ntfy_url("https://example.com/ntfy/abc/") == ("https://example.com/ntfy", "abc")
    assert split_ntfy_url("https://ntfy.sh") == ("https://ntfy.sh", "")


# ---------------------------------------------------------------- test send against stubs
def test_send_test_alert_success_and_failure(tmp_path):
    async def fn(eng, c):
        runner, base, got = await http_stub()
        bad_runner, bad_base, _ = await http_stub(statuses=[401])
        smtp, smtp_port, mails = await smtp_stub()
        try:
            r = await c.post("/api/alerts/channels/ntfy/test")
            assert r.status == 400 and "topic missing" in (await r.json())["error"]

            await c.post("/api/alerts/channels/ntfy", json={"server": base, "topic": "sentry-abc"})
            res = await (await c.post("/api/alerts/channels/ntfy/test")).json()   # works before enabling
            assert res["ok"] is True and res["attempts"] == 1 and res["channel"] == "ntfy"
            assert got[-1]["path"] == "/sentry-abc" and got[-1]["headers"]["Title"] == "wifi-sentry test alert"

            await c.post("/api/alerts/channels/ntfy", json={"server": bad_base, "token": "wrong"})
            res = await (await c.post("/api/alerts/channels/ntfy/test")).json()
            assert res["ok"] is False and "answered HTTP 401: login or token refused" in res["error"] and "stub says no" not in res["error"]
            ch = channel(await (await c.get("/api/alerts")).json(), "ntfy")
            assert ch["last"]["ok"] is False and ch["last"]["kind"] == "test" and ch["last_ok"] is not None

            await c.post("/api/alerts/channels/webhook", json={"url": base + "/hook"})
            res = await (await c.post("/api/alerts/channels/webhook/test")).json()
            assert res["ok"] and json.loads(got[-1]["body"])["kind"] == "test"

            await c.post("/api/alerts/channels/email", json={"host": "127.0.0.1", "port": smtp_port, "security": "none",
                                                             "from": "s@example.com", "to": "me@example.com"})
            res = await (await c.post("/api/alerts/channels/email/test")).json()
            assert res["ok"] and "Subject: wifi-sentry test alert" in mails[-1]
            await c.post("/api/alerts/channels/email", json={"security": "starttls"})
            res = await (await c.post("/api/alerts/channels/email/test")).json()
            assert res["ok"] is False and "STARTTLS extension not supported" in res["error"]
            # a failed *test* is not a delivery failure of real alerts
            assert eng.alerts_summary()["failing"] == []
        finally:
            await runner.cleanup()
            await bad_runner.cleanup()
            smtp.close()
            await smtp.wait_closed()
    run_with(tmp_path, fn)


def test_armed_motion_reaches_the_phone_and_failures_show(tmp_path, idle_rows):
    async def fn(eng, c):
        runner, base, got = await http_stub()
        try:
            await c.post("/api/alerts/channels/ntfy", json={"server": base, "topic": "sentry-xyz", "enabled": True})
            await c.post("/api/alerts/settings", json={"base_url": "http://192.0.2.5:8450/"})
            await c.post("/api/arm", json={"armed": True})
            rows, shift = scenario(idle_rows)
            feed(eng, rows, shift)
            await eng.router.drain()
            assert len(got) == 1                          # one event -> one push
            assert got[0]["headers"]["Title"] == "Motion detected at home"
            assert got[0]["headers"]["Click"] == "http://192.0.2.5:8450/"
            body = got[0]["body"].decode()
            assert "Motion started at" in body and "Dashboard: http://192.0.2.5:8450/" in body
            s = await (await c.get("/api/status")).json()
            assert s["alerts"] == {"channels": ["ntfy"], "failing": []}
            # the server goes away: the next armed event fails visibly (after retries), detection carries on
            await runner.cleanup()
            await c.post("/api/alerts/settings", json={"cooldown_s": 0})
            feed(eng, rows, shift + 200)
            await eng.router.drain()
            s = await (await c.get("/api/status")).json()
            assert s["alerts"]["failing"] and s["alerts"]["failing"][0]["channel"] == "ntfy"
            assert "couldn't connect" in s["alerts"]["failing"][0]["error"]
            assert eng.router.results["ntfy"]["attempts"] == 3 and s["state"] in ("clear", "motion")
            # persisted for the next start
            eng.save_state()
            assert eng.storage.kv_get("alert_state")["results"]["ntfy"]["ok"] is False
        finally:
            await runner.cleanup()
    run_with(tmp_path, fn)


# ---------------------------------------------------------------- onboarding
def test_onboarding_fresh_install_is_resumable_and_skippable(tmp_path):
    async def fn(eng, c):
        ob = await (await c.get("/api/onboarding")).json()
        assert ob["show"] is True and ob["status"] == "pending" and ob["step"] == "intro"
        assert ob["steps"] == ["intro", "sensor", "learning", "walk", "alerts", "arm"]
        assert ob["alerts_prompt"] is False             # the setup itself covers alerts
        ob = await (await c.post("/api/onboarding", json={"status": "active", "step": "walk"})).json()
        assert ob["show"] and ob["step"] == "walk"
        assert (await c.post("/api/onboarding", json={"step": "teleport"})).status == 400
        assert (await c.post("/api/onboarding", json={"status": "pending"})).status == 400
    run_with(tmp_path, fn)

    async def resumed(eng, c):                           # restart: same place
        ob = await (await c.get("/api/onboarding")).json()
        assert ob["show"] and ob["step"] == "walk" and ob["status"] == "active"
        ob = await (await c.post("/api/onboarding", json={"status": "skipped"})).json()
        assert ob["show"] is False and ob["alerts_prompt"] is True
        ob = await (await c.post("/api/onboarding", json={"alerts_prompt_dismissed": True})).json()
        assert ob["alerts_prompt"] is False
        ob = await (await c.post("/api/onboarding", json={"status": "active", "step": "intro"})).json()  # run again
        assert ob["show"] and ob["step"] == "intro"
    run_with(tmp_path, resumed)


def test_existing_install_with_events_is_not_onboarded(tmp_path, idle_rows):
    async def make_events(eng, c):
        await c.post("/api/onboarding", json={"status": "skipped"})
        rows, shift = scenario(idle_rows)
        feed(eng, rows, shift)
        assert eng.storage.last_event() is not None
    run_with(tmp_path, make_events)
    # an install from before onboarding existed: events, no onboarding record
    db = make_engine(tmp_path)
    db.storage.kv_set("onboarding", None)
    db.storage.close()

    async def check(eng, c):
        ob = await (await c.get("/api/onboarding")).json()
        assert ob["status"] == "existing" and ob["show"] is False
        assert ob["alerts_prompt"] is True               # at most the small "set up phone alerts" prompt
        await c.post("/api/alerts/channels/webhook", json={"url": "http://127.0.0.1:9/x", "enabled": True})
        assert (await (await c.get("/api/onboarding")).json())["alerts_prompt"] is False
    run_with(tmp_path, check)


def test_status_carries_arm_and_alert_summary(tmp_path):
    async def fn(eng, c):
        s = await (await c.get("/api/status")).json()
        assert s["arm"]["mode"] == "manual" and s["arm"]["delay_s"] == 60 and s["arm"]["next_change"] is None
        assert s["alerts"] == {"channels": [], "failing": []}
        d = await (await c.get("/api/debug")).json()
        assert d["sinks"] == []
        assert time.time() > 0
    run_with(tmp_path, fn)
