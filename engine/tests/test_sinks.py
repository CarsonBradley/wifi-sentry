"""Alert channels against LOCAL stub servers only (no real notification is ever sent, no real network)."""
import asyncio
import base64
import json

from aiohttp import web

from sentry.sinks import (AlertRouter, DeliveryError, EmailSink, NtfySink, Sink, WebhookSink, build_sinks, compose,
                          deliver)

from conftest import cfg_with

FAST = (0.01, 0.02)  # retry backoff in tests


async def http_stub(statuses=None, delay=0.0, body="ok"):
    """POST sink. `statuses`: list of HTTP codes to answer in turn (then 200)."""
    got = []
    queue = list(statuses or [])

    async def handler(request):
        got.append({"path": request.path, "headers": dict(request.headers), "body": await request.read()})
        if delay:
            await asyncio.sleep(delay)
        code = queue.pop(0) if queue else 200
        if code >= 300:
            return web.json_response({"code": code * 100 + 1, "http": code, "error": f"stub says no ({code})"},
                                     status=code)
        return web.Response(text=body)

    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", got


async def smtp_stub(auth: str | None = None):
    """Just enough SMTP for smtplib: EHLO/HELO, AUTH (optional: auth="ok" | "refuse"), MAIL, RCPT, DATA, QUIT.
    No STARTTLS: asking for it gets smtplib's real 'not supported' error."""
    msgs = []

    async def handle(reader, writer):
        writer.write(b"220 stub ESMTP\r\n")
        data_mode, buf = False, []
        while True:
            line = await reader.readline()
            if not line:
                break
            if data_mode:
                if line in (b".\r\n", b".\n"):
                    data_mode = False
                    msgs.append(b"".join(buf).decode())
                    buf = []
                    writer.write(b"250 queued\r\n")
                else:
                    buf.append(line)
                continue
            cmd = line[:4].upper()
            if cmd == b"EHLO":
                writer.write(b"250-stub\r\n" + (b"250-AUTH PLAIN LOGIN\r\n" if auth else b"") + b"250 8BITMIME\r\n")
            elif cmd == b"AUTH":
                writer.write(b"235 ok\r\n" if auth == "ok" else b"535 5.7.8 bad credentials\r\n")
            elif cmd == b"DATA":
                data_mode = True
                writer.write(b"354 go\r\n")
            elif cmd == b"QUIT":
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 ok\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], msgs


EV = {"id": 7, "start": 1795400000.0, "end": None, "duration_s": 3.0, "peak_score": 6.5, "threshold": 2.0,
      "sources": ["mac"], "armed": True, "label": None, "note": "", "walktest": False}
STATUS = {"state": "motion", "armed": True, "score": 6.5, "threshold": 3.0, "sensitivity": "medium"}


class Capture(Sink):
    """In-memory channel: records what would have been sent."""
    id = "capture"

    def __init__(self, fail=0, exc=None):
        self.sent, self.fail, self.exc, self.calls = [], fail, exc, 0

    async def send(self, alert, status):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        if self.calls <= self.fail:
            raise DeliveryError(f"temporary failure {self.calls}")
        self.sent.append(alert)


def test_sinks_disabled_by_default():
    assert build_sinks(cfg_with()) == []


def test_compose_is_generic_and_complete():
    a = compose("open", EV, now=EV["start"] + 3)
    assert a["title"] == "Motion detected at home"
    assert "strong (3.2x the trigger level)" in a["message"] and "Going on for 3 s so far" in a["message"]
    assert "mac" not in a["message"] and a["url"] is None
    a = compose("open", EV, suppressed=2, cooldown_s=300, base_url="http://192.0.2.5:8450/", now=EV["start"] + 3)
    assert "2 more motion events since the previous alert were not sent separately" in a["message"]
    assert "at least 5 min apart" in a["message"] and a["message"].endswith("Dashboard: http://192.0.2.5:8450/")
    assert a["url"] == "http://192.0.2.5:8450/" and a["suppressed"] == 2
    c = compose("close", dict(EV, end=EV["start"] + 75, duration_s=75.0))
    assert c["title"] == "Motion ended at home" and "after 1 min 15 s" in c["message"] and c["suppressed"] == 0
    t = compose("test", None)
    assert t["title"] == "wifi-sentry test alert" and t["event"] is None


def test_router_and_sinks_against_stubs():
    async def run():
        runner, base, got = await http_stub()
        server, smtp_port, mails = await smtp_stub()
        try:
            sinks = [NtfySink(base + "/ntfy-topic", "high"), WebhookSink(base + "/hook"),
                     EmailSink("127.0.0.1", smtp_port, "none", "", "", "sentry@localhost", ["me@localhost"])]
            r = AlertRouter(sinks, cooldown_s=60, notify_on_close=False, backoff=FAST)
            assert r.handle("open", dict(EV, armed=False, id=1), STATUS, 1000.0) is False  # disarmed
            assert r.handle("open", dict(EV, walktest=True, id=2), STATUS, 1000.0) is False  # walk test
            assert r.handle("open", EV, STATUS, 1000.0) is True
            assert r.handle("open", EV, STATUS, 1001.0) is False  # same event (merge re-open)
            assert r.handle("open", dict(EV, id=8), STATUS, 1030.0) is False  # cooldown
            assert r.handle("close", dict(EV, end=1795400010.0), STATUS, 1031.0) is False  # close notify off
            await r.drain()
            assert [g["path"] for g in sorted(got, key=lambda g: g["path"])] == ["/hook", "/ntfy-topic"]
            ntfy = next(g for g in got if g["path"] == "/ntfy-topic")
            assert ntfy["headers"]["Title"] == "Motion detected at home"
            assert ntfy["headers"]["Priority"] == "high" and ntfy["headers"]["Tags"] == "rotating_light"
            assert b"Motion started at" in ntfy["body"]
            hook = json.loads(next(g for g in got if g["path"] == "/hook")["body"])
            assert hook["kind"] == "open" and hook["event"]["id"] == 7 and hook["source"] == "wifi-sentry"
            assert hook["title"] == "Motion detected at home" and hook["status"]["armed"] is True
            assert len(mails) == 1 and "Subject: Motion detected at home" in mails[0]
            assert all(rec["ok"] for rec in r.log) and set(r.results) == {"ntfy", "webhook", "email"}
            assert r.suppressed == 1

            # failures are recorded, not raised
            r2 = AlertRouter([WebhookSink("http://127.0.0.1:9/nothing-listens")], cooldown_s=0, backoff=FAST)
            assert r2.handle("open", dict(EV, id=9), STATUS, 2000.0) is True
            await r2.drain()
            assert r2.log and r2.log[0]["ok"] is False and r2.log[0]["attempts"] == 3
            assert "couldn't connect to 127.0.0.1:9" in r2.log[0]["error"]
        finally:
            await runner.cleanup()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_retries_with_backoff_then_success():
    async def run():
        runner, base, got = await http_stub(statuses=[503, 502])
        try:
            res = await deliver(WebhookSink(base + "/h"), compose("test", None), STATUS, attempts=3, backoff=FAST)
            assert res["ok"] is True and res["attempts"] == 3 and len(got) == 3
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_no_retry_when_it_cannot_help_and_real_error_text():
    async def run():
        runner, base, got = await http_stub(statuses=[403])
        try:
            res = await deliver(NtfySink(base + "/topic"), compose("test", None), STATUS, attempts=3, backoff=FAST)
            assert res["ok"] is False and res["attempts"] == 1 and len(got) == 1
            assert res["error"].endswith("answered HTTP 403: refused (forbidden; check the token or topic)")
            assert "stub says no" not in res["error"]  # response bodies are never echoed
            # 429 and 5xx are worth retrying
            runner2, base2, got2 = await http_stub(statuses=[429, 500, 500])
            res = await deliver(NtfySink(base2 + "/topic"), compose("test", None), STATUS, attempts=3, backoff=FAST)
            assert res["ok"] is False and res["attempts"] == 3 and "HTTP 500" in res["error"]
            await runner2.cleanup()
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_timeout_is_reported():
    async def run():
        runner, base, got = await http_stub(delay=2.0)
        try:
            res = await deliver(WebhookSink(base + "/slow", timeout=0.3), compose("test", None), STATUS,
                                attempts=2, backoff=FAST)
            assert res["ok"] is False and res["attempts"] == 2 and "no answer from 127.0.0.1" in res["error"]
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_a_crashing_channel_never_takes_the_router_down():
    async def run():
        bad = Capture(exc=ZeroDivisionError("boom"))
        results = []
        r = AlertRouter([bad, Capture()], cooldown_s=0, backoff=FAST, on_result=results.append)
        assert r.handle("open", EV, STATUS, 1.0) is True
        await r.drain()
        bad_rec = next(x for x in results if x["error"])
        assert bad_rec["attempts"] == 3 and bad_rec["error"] == "ZeroDivisionError: boom"
        assert sum(x["ok"] for x in results) == 1
    asyncio.run(run())


def test_cooldown_suppression_count_and_one_alert_per_event():
    async def run():
        cap = Capture()
        r = AlertRouter([cap], cooldown_s=300, backoff=FAST)
        assert r.handle("open", dict(EV, id=1), STATUS, 1000.0)
        assert not r.handle("open", dict(EV, id=1), STATUS, 1001.0)    # same event again
        assert not r.handle("open", dict(EV, id=2), STATUS, 1100.0)    # cooldown: held back
        assert not r.handle("open", dict(EV, id=3), STATUS, 1299.0)
        assert not r.handle("open", dict(EV, id=4, armed=False), STATUS, 1299.5)  # disarmed: not counted
        assert r.suppressed == 2
        assert r.handle("open", dict(EV, id=5), STATUS, 1300.0)        # cooldown over
        await r.drain()
        assert [a["event"]["id"] for a in cap.sent] == [1, 5]
        assert cap.sent[0]["suppressed"] == 0 and "not sent separately" not in cap.sent[0]["message"]
        assert cap.sent[1]["suppressed"] == 2 and "2 more motion events" in cap.sent[1]["message"]
        assert r.suppressed == 0
        # the counters survive a restart
        r2 = AlertRouter([cap], cooldown_s=300)
        r2.import_state(json.loads(json.dumps(r.export_state())))
        assert not r2.handle("open", dict(EV, id=5), STATUS, 1301.0)   # already alerted
        assert not r2.handle("open", dict(EV, id=6), STATUS, 1400.0) and r2.suppressed == 1
        # cooldown 0: every event alerts
        r3 = AlertRouter([cap], cooldown_s=0)
        assert r3.handle("open", dict(EV, id=1), STATUS, 1.0) and r3.handle("open", dict(EV, id=2), STATUS, 1.0)
        await r3.drain()
    asyncio.run(run())


def test_all_clear_only_when_enabled_and_after_an_alert():
    async def run():
        cap = Capture()
        r = AlertRouter([cap], cooldown_s=300, notify_on_close=True, backoff=FAST)
        assert r.handle("open", dict(EV, id=1), STATUS, 0.0)
        assert r.handle("close", dict(EV, id=1, end=EV["start"] + 40, duration_s=40.0), STATUS, 40.0)
        assert not r.handle("open", dict(EV, id=2), STATUS, 60.0)                     # suppressed
        assert not r.handle("close", dict(EV, id=2, end=EV["start"] + 90), STATUS, 90.0)  # never alerted
        await r.drain()
        assert [a["kind"] for a in cap.sent] == ["open", "close"]
        assert cap.sent[1]["title"] == "Motion ended at home"
    asyncio.run(run())


def test_retry_in_router_records_attempts():
    async def run():
        flaky = Capture(fail=2)
        r = AlertRouter([flaky], cooldown_s=0, backoff=FAST)
        r.handle("open", EV, STATUS, 1.0)
        await r.drain()
        assert r.results["capture"]["ok"] and r.results["capture"]["attempts"] == 3
        res = await r.test(Capture(fail=1))   # a test is a single attempt: the real error at once
        assert res["ok"] is False and res["attempts"] == 1 and res["error"] == "temporary failure 1"
    asyncio.run(run())


def test_ntfy_headers_auth_and_click():
    a = compose("open", EV, base_url="http://192.0.2.5:8450/")
    h = NtfySink("https://ntfy.example/t", "urgent", "tk_abc").headers(a)
    assert h["Authorization"] == "Bearer tk_abc" and h["Click"] == "http://192.0.2.5:8450/" and h["Priority"] == "urgent"
    h = NtfySink("https://ntfy.example/t", "high", "me:pw").headers(compose("close", dict(EV, end=EV["start"] + 1)))
    assert h["Authorization"] == "Basic " + base64.b64encode(b"me:pw").decode() and h["Priority"] == "default"
    assert "Click" not in h


def test_webhook_formats():
    a = compose("open", EV)
    assert WebhookSink("http://x/", "slack").payload(a, STATUS) == {"text": a["title"] + "\n" + a["message"]}
    assert WebhookSink("http://x/", "discord").payload(a, STATUS) == {"content": a["title"] + "\n" + a["message"]}
    j = WebhookSink("http://x/", "json").payload(a, STATUS)
    assert set(j) == {"source", "kind", "title", "message", "time", "url", "suppressed", "event", "status"}


def test_email_success_and_real_errors():
    async def run():
        ok_srv, ok_port, mails = await smtp_stub(auth="ok")
        no_srv, no_port, _ = await smtp_stub(auth="refuse")
        try:
            t = compose("test", None)
            res = await deliver(EmailSink("127.0.0.1", ok_port, "none", "me", "pw", "a@b.example", ["c@d.example"]),
                                t, STATUS, attempts=1)
            assert res["ok"] and "Subject: wifi-sentry test alert" in mails[0] and "To: c@d.example" in mails[0]
            res = await deliver(EmailSink("127.0.0.1", no_port, "none", "me", "bad", "a@b.example", ["c@d.example"]),
                                t, STATUS, attempts=3, backoff=FAST)
            assert not res["ok"] and res["attempts"] == 1   # a refused login is not retried
            assert res["error"] == f"127.0.0.1:{no_port} refused the login: 535 5.7.8 bad credentials"
            res = await deliver(EmailSink("127.0.0.1", ok_port, "starttls", "", "", "a@b.example", ["c@d.example"]),
                                t, STATUS, attempts=3, backoff=FAST)
            assert not res["ok"] and res["attempts"] == 1 and "STARTTLS extension not supported" in res["error"]
            res = await deliver(EmailSink("127.0.0.1", 9, "none", "", "", "a@b.example", ["c@d.example"]),
                                t, STATUS, attempts=2, backoff=FAST)
            assert not res["ok"] and res["attempts"] == 2 and res["error"].startswith("couldn't connect to 127.0.0.1:9")
        finally:
            for s in (ok_srv, no_srv):
                s.close()
                await s.wait_closed()
    asyncio.run(run())
