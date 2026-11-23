"""Fixes from the 0.3.0 security review (items M2, M3a, M4, L1, L5, L6; M1/L2/L3 are in test_auth.py,
the ESP32 items in test_esp32_security.py). No network beyond 127.0.0.1."""
import asyncio
import json
import os
import stat

import pytest

import sentry.sinks as sinks
from sentry.alertcfg import AlertConfig
from sentry.config import PROJECT_ROOT
from sentry.sinks import AlertRouter, DeliveryError, _host, check_target, compose, refused_address, scrub

from conftest import cfg_with
from test_api import _client, feed, make_engine
from test_sinks import EV, STATUS, Capture

FAST = (0.01, 0.02)


# ---------------------------------------------------------------- M4: secrets never echoed
def test_host_is_host_and_port_only():
    assert _host("https://user:secret@hooks.example.com:8443/services/T0/B0/XXXX?x=1") == "hooks.example.com:8443"
    assert _host("https://ntfy.sh/sentry-topic") == "ntfy.sh"
    assert _host("http://[2001:db8::1]:8080/x") == "[2001:db8::1]:8080"


def test_old_results_are_scrubbed_on_import():
    r = AlertRouter([])
    r.import_state({"last_sent": 5.0, "suppressed": 2, "results": {"webhook": {
        "ok": False, "error": "not a valid URL: https://user:pw@hooks.example.com/secret/path?k=v", "t": 1.0}}})
    err = r.results["webhook"]["error"]
    assert "pw" not in err and "secret" not in err and "hooks.example.com/…" in err
    assert r.last_sent_by == {"mac": 5.0} and r.suppressed == 2          # 0.2-style state migrated
    assert scrub(None) is None


@pytest.mark.parametrize("url,msg", [
    ("http://user:pw@hooks.example.com/x", "user name or password"),
    ("http://hooks.example.com:99999/x", "invalid port"),
    ("http://169.254.169.254/latest/meta-data", "link-local"),
    ("http://[fe80::1]/x", "link-local"),
    ("http://0.0.0.0:8450/api/arm", "link-local"),
])
def test_alert_urls_are_validated_on_save(tmp_path, url, msg):
    a = AlertConfig(tmp_path / "alerts.json", cfg_with())
    with pytest.raises(ValueError, match=msg.split()[0]):
        a.update_channel("webhook", {"url": url})


def test_loopback_targets_are_refused_outside_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(sinks, "ALLOW_LOOPBACK_TARGETS", False)
    a = AlertConfig(tmp_path / "alerts.json", cfg_with())
    for url in ("http://127.0.0.1:8450/api/arm", "http://localhost/x", "http://[::1]/x", "http://[::ffff:127.0.0.1]/"):
        with pytest.raises(ValueError, match="this Mac itself"):
            a.update_channel("webhook", {"url": url})
    a.update_channel("webhook", {"url": "http://192.168.1.10:8123/api/webhook/abc"})   # LAN (Home Assistant) is fine
    assert refused_address("127.0.0.1", 80) and refused_address("169.254.169.254", 80)
    assert refused_address("192.168.1.10", 8123) is None


def test_names_resolving_to_loopback_or_to_the_engine_itself_are_refused_at_send(monkeypatch):
    async def run():
        monkeypatch.setattr(sinks, "ALLOW_LOOPBACK_TARGETS", False)
        with pytest.raises(DeliveryError, match="loopback"):
            await check_target("http://localhost:9/x")
        monkeypatch.setattr(sinks, "ALLOW_LOOPBACK_TARGETS", True)
        monkeypatch.setattr(sinks, "SELF_BOUND", {("127.0.0.1", 8450)})
        with pytest.raises(DeliveryError, match="wifi-sentry itself"):
            await check_target("http://127.0.0.1:8450/api/arm")
        await check_target("http://127.0.0.1:8451/x")                      # another local port (tests only)
    asyncio.run(run())


def test_debug_and_alerts_api_hold_no_secrets(tmp_path):
    async def run():
        eng = make_engine(tmp_path)
        c = await _client(eng)
        try:
            secret_url = "https://hooks.example.com/services/T000/B000/TOPSECRETPATH"
            await c.post("/api/alerts/channels/webhook", json={"url": secret_url})
            await c.post("/api/alerts/channels/ntfy", json={"topic": "sentry-secret-topic-x", "token": "tk_SECRET1"})
            await c.post("/api/alerts/channels/email", json={"host": "smtp.example.com", "password": "SMTPSECRET",
                                                             "from": "a@b.example", "to": "c@d.example"})
            eng.router.import_state({"results": {"webhook": {"ok": False, "error": "x " + secret_url, "t": 1.0}}})
            text = ""
            for path in ("/api/alerts", "/api/debug", "/api/status"):
                text += await (await c.get(path)).text()
            for s in ("TOPSECRETPATH", "tk_SECRET1", "SMTPSECRET"):
                assert s not in text
            eng.save_state()
            db = (eng.data_dir / "sentry.db").read_bytes() + (eng.data_dir / "sentry.db-wal").read_bytes() \
                if (eng.data_dir / "sentry.db-wal").exists() else (eng.data_dir / "sentry.db").read_bytes()
            assert b"TOPSECRETPATH" not in db
        finally:
            await c.close()
            await eng.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- M3a: cooldown per sensor class
def test_esp32_only_events_never_mute_the_mac_sensor():
    async def run():
        cap = Capture()
        r = AlertRouter([cap], cooldown_s=300, backoff=FAST)
        forged = dict(EV, id=1, sources=["esp32:fake1"])
        assert r.handle("open", forged, STATUS, 1000.0) is True
        assert r.handle("open", dict(forged, id=2), STATUS, 1010.0) is False         # esp32 cooldown
        assert r.handle("open", dict(EV, id=3), STATUS, 1020.0) is True              # the Mac's own: alerted
        assert r.handle("open", dict(EV, id=4), STATUS, 1030.0) is False             # mac cooldown
        await r.drain()
        assert [a["event"]["id"] for a in cap.sent] == [1, 3]
        assert r.suppressed_by == {"esp32": 1, "mac": 1}
    asyncio.run(run())


def test_the_mac_sensor_joining_an_esp32_event_alerts_again():
    async def run():
        cap = Capture()
        r = AlertRouter([cap], cooldown_s=300, backoff=FAST)
        ev = dict(EV, id=9, sources=["esp32:fake1"])
        assert r.handle("open", ev, STATUS, 1000.0) is True
        assert r.handle("update", dict(ev, peak_score=40.0), STATUS, 1005.0) is False   # same boards: no
        assert r.handle("update", dict(ev, sources=["esp32:fake1", "mac"]), STATUS, 1010.0) is True
        assert r.handle("update", dict(ev, sources=["esp32:fake1", "mac"]), STATUS, 1011.0) is False  # once
        # an event the boards opened during their cooldown (held back) still alerts when the Mac joins
        ev2 = dict(EV, id=10, sources=["esp32:fake1"])
        r2 = AlertRouter([cap], cooldown_s=300, backoff=FAST)
        r2.last_sent_by["esp32"] = 999.0
        assert r2.handle("open", ev2, STATUS, 1000.0) is False
        assert r2.handle("update", dict(ev2, sources=["esp32:fake1", "mac"]), STATUS, 1001.0) is True
        # a Mac event that ESP32 boards join is not re-alerted
        ev3 = dict(EV, id=11)
        assert r2.handle("open", ev3, STATUS, 1200.0) is False                        # mac cooldown (1001)
        assert r2.handle("update", dict(ev3, sources=["esp32:x", "mac"]), STATUS, 1201.0) is False
        await r.drain()
        await r2.drain()
    asyncio.run(run())


# ---------------------------------------------------------------- M2: armed but not watching
def test_health_alerts_have_their_own_cooldown():
    async def run():
        cap = Capture()
        r = AlertRouter([cap], cooldown_s=300, backoff=FAST, health_cooldown_s=1800)
        assert r.handle("open", EV, STATUS, 1000.0) is True
        assert r.handle_health("blind", {"since": 990.0, "reason": "sensor offline"}, STATUS, 1001.0) is True
        assert r.handle_health("watching", {"since": 990.0, "until": 1100.0}, STATUS, 1100.0) is True
        assert r.handle_health("blind", {"since": 1150.0, "reason": "x"}, STATUS, 1200.0) is False   # flapping
        await r.drain()
        kinds = [a["kind"] for a in cap.sent]
        assert kinds == ["open", "blind", "watching"]
        assert cap.sent[1]["title"] == "wifi-sentry can't sense" and "sensor offline" in cap.sent[1]["message"]
        assert cap.sent[1]["event"] is None
    asyncio.run(run())


def test_engine_alerts_when_armed_and_not_watching_then_when_watching_again(tmp_path, idle_rows):
    async def run():
        eng = make_engine(tmp_path)
        cap = Capture()
        eng.router.sinks = [cap]
        try:
            t0 = float(int(idle_rows[0]["t"])) + 10_000
            eng.set_armed(True)
            eng.tick(t0)
            eng.tick(t0 + 60)
            assert cap.calls == 0                                   # not yet: 120 s by default
            eng.tick(t0 + 125)
            await eng.router.drain()
            assert [a["kind"] for a in cap.sent] == ["blind"]
            assert "offline" in cap.sent[0]["message"] or "sensor" in cap.sent[0]["message"]
            feed(eng, idle_rows[:900], t0 + 130 - idle_rows[0]["t"])  # 60 s learning, then clear
            await eng.router.drain()
            assert eng.detector.state == "clear"
            assert [a["kind"] for a in cap.sent] == ["blind", "watching"]
        finally:
            await eng.stop()
    asyncio.run(run())


def test_disarmed_or_disabled_means_no_not_watching_alert(tmp_path):
    async def run():
        eng = make_engine(tmp_path, alerts={"not_watching_s": 0})
        cap = Capture()
        eng.router.sinks = [cap]
        try:
            eng.set_armed(True)
            for k in range(0, 600, 30):
                eng.tick(1_800_000_000.0 + k)
            eng2_cap = cap.calls
        finally:
            await eng.stop()
        eng = make_engine(tmp_path / "b")
        eng.router.sinks = [cap]
        try:
            for k in range(0, 600, 30):                              # disarmed
                eng.tick(1_800_000_000.0 + k)
            await eng.router.drain()
            assert eng2_cap == 0 and cap.calls == 0
        finally:
            await eng.stop()
    asyncio.run(run())


def test_engine_down_while_armed_is_reported_at_start(tmp_path):
    import time as _t

    async def run():
        eng = make_engine(tmp_path)
        eng.set_armed(True)
        await eng.stop()
        from sentry.storage import Storage
        st = Storage(tmp_path / "data" / "sentry.db")
        st.kv_set("heartbeat", _t.time() - 900)                    # down for 15 min
        st.close()
        eng = make_engine(tmp_path)
        cap = Capture()
        eng.router.sinks = [cap]
        try:
            await eng.start()
            await eng.router.drain()
            assert [a["kind"] for a in cap.sent] == ["restart"]
            assert "not running" in cap.sent[0]["title"]
        finally:
            await eng.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- L1: owner-only files
def test_data_dir_and_files_are_made_owner_only(tmp_path):
    d = tmp_path / "data"
    (d / "raw").mkdir(parents=True)
    (d / "raw" / "raw-20261101-01.jsonl").write_text("{}\n")
    os.chmod(d / "raw" / "raw-20261101-01.jsonl", 0o644)
    os.chmod(d, 0o755)
    eng = make_engine(tmp_path)
    try:
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(d / "raw").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(d / "raw" / "raw-20261101-01.jsonl").st_mode) == 0o600
    finally:
        eng.storage.close()


# ---------------------------------------------------------------- L5: only dashboard assets are served
def test_static_files_are_an_allowlist(tmp_path):
    async def run():
        eng = make_engine(tmp_path, web_dir=PROJECT_ROOT / "web")
        c = await _client(eng)
        try:
            for path in ("/", "/index.html", "/app.js", "/style.css", "/manifest.json", "/icon.svg"):
                assert (await c.get(path, headers={"Authorization": ""})).status == 200, path
            for path in ("/dev/flows.py", "/dev/", "/dev/mock_server.py", "/dev/screenshots/x.png", "/.DS_Store",
                         "/../config.local.toml", "/%2e%2e/config.toml", "/README.md"):
                assert (await c.get(path, headers={"Authorization": ""})).status == 404, path
        finally:
            await c.close()
            await eng.stop()
    asyncio.run(run())


def test_alert_texts_stay_generic():
    for kind, info in (("blind", {"since": 1.0, "reason": "Wi-Fi is turned off on this Mac"}),
                       ("watching", {"since": 1.0, "until": 100.0}), ("restart", {"since": 1.0, "until": 700.0})):
        a = compose(kind, info, now=800.0)
        assert a["title"].startswith("wifi-sentry") and a["event"] is None
        json.dumps(a)


# ---------------------------------------------------------------- checkout paths with spaces and '&'
def test_launchagent_ownership_survives_odd_checkout_paths(tmp_path, monkeypatch):
    """The plist escapes '&' as &amp;, so a grep for the raw path never matched (review: informational)."""
    import argparse
    import sentry.cli as cli
    root = tmp_path / "my stuff & wifi-sentry (v1.0) [x]*"
    root.mkdir()
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    monkeypatch.setattr(cli, "PLIST", tmp_path / "LaunchAgents" / "com.wifi-sentry.engine.plist")
    cfg = cfg_with(paths={"data_dir": str(root / "data")})
    assert cli.cmd_launchagent(cfg, argparse.Namespace(uv_dir="/opt/x")) == 0
    assert "&amp;" in cli.PLIST.read_text()
    assert cli.agent_is_mine()
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "my stuff")      # a prefix of it is not "mine"
    assert not cli.agent_is_mine()


def test_cli_commands_run_and_print_the_tokenised_local_link(tmp_path, capsys):
    from sentry import cli
    conf = tmp_path / "c.toml"
    conf.write_text(f'[server]\nport = 1\n[paths]\ndata_dir = "{tmp_path / "data"}"\n')
    assert cli.main(["--config", str(conf), "info"]) == 0
    assert cli.main(["--config", str(conf), "open-url"]) == 0
    tok = (tmp_path / "data" / "access-token").read_text().strip()
    assert capsys.readouterr().out.strip().endswith(f"http://127.0.0.1:1/?token={tok}")
    assert cli.main(["--config", str(conf), "url"]) == 0
    out = capsys.readouterr().out
    assert f"On this Mac:  http://127.0.0.1:1/?token={tok}" in out and "not running" in out
    assert cli.main(["--config", str(conf), "status"]) == 3
    assert cli.main(["--config", str(conf), "agent-mine"]) in (0, 1)


def test_loopback_targets_are_a_config_file_opt_in(tmp_path):
    eng = make_engine(tmp_path, alerts={"allow_loopback_targets": False})
    try:
        assert sinks.ALLOW_LOOPBACK_TARGETS is False
        with pytest.raises(ValueError, match="this Mac itself"):
            eng.alert_cfg.update_channel("webhook", {"url": "http://127.0.0.1:8787/anything"})
        with pytest.raises(ValueError, match="unknown"):
            eng.alert_cfg.update_settings({"allow_loopback_targets": True})   # not settable over the API
    finally:
        eng.storage.close()


def test_a_long_pause_between_ticks_while_armed_is_reported(tmp_path):
    """The Mac slept: the process lived on, but nothing was watched."""
    async def run():
        eng = make_engine(tmp_path)
        cap = Capture()
        eng.router.sinks = [cap]
        try:
            eng.set_armed(True)
            eng.tick(1_800_000_000.0)
            eng.tick(1_800_000_001.0)
            eng.tick(1_800_003_600.0)                                # an hour later
            await eng.router.drain()
            assert [a["kind"] for a in cap.sent] == ["restart"] and "asleep" in cap.sent[0]["message"]
        finally:
            await eng.stop()
    asyncio.run(run())
