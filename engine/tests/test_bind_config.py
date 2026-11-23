"""Where the HTTP server listens and config migration from 0.1.x. No real network beyond 127.0.0.1 / ::1.
(That the committed tree carries nothing specific to one machine: test_repo_hygiene.py.)"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

import sentry.config as C
from sentry import net
from sentry.config import PROJECT_ROOT, bind_specs, load_config
from sentry.storage import Storage

from conftest import cfg_with
from test_api import feed, make_engine


# ---------------------------------------------------------------- bind specs
def test_default_is_loopback_only():
    cfg = load_config(PROJECT_ROOT / "config.toml")
    assert bind_specs(cfg) == ["loopback"]
    assert cfg["esp32"]["enabled"] is False and cfg["esp32"]["bind"] == "lan"


def test_expose_adds_to_loopback():
    cfg = cfg_with(server={"expose": ["tailscale", "lan", "192.0.2.7", "tailscale"]})
    assert bind_specs(cfg) == ["loopback", "tailscale", "lan", "192.0.2.7"]


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "*", "", "my-laptop.local", 7])
def test_wildcards_and_junk_are_refused(bad):
    with pytest.raises(ValueError):
        cfg_with(server={"expose": [bad]})


def test_esp32_bind_validation():
    assert cfg_with(esp32={"bind": "0.0.0.0"})["esp32"]["bind"] == "0.0.0.0"   # explicit, documented
    with pytest.raises(ValueError):
        cfg_with(esp32={"bind": "everywhere"})


def test_cli_hosts_override_replaces_the_list():
    cfg = cfg_with(server={"expose": ["tailscale"], "bind_override": ["loopback"]})
    assert bind_specs(cfg) == ["loopback"] and cfg["_notes"] == []


# ---------------------------------------------------------------- config migration (0.1.x files)
def test_legacy_config_keeps_working(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[server]\nhosts = ["127.0.0.1", "100.64.102.103"]\nport = 8450\n'
        '[esp32]\nenabled = true\nbind = "0.0.0.0"\n')
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    cfg = C.load_config()
    assert bind_specs(cfg) == ["loopback", "100.64.102.103"]          # same addresses as before
    assert any('expose = ["tailscale"]' in n for n in cfg["_notes"])
    assert any("every interface" in n for n in cfg["_notes"])


def test_local_overlay_turns_on_tailscale(tmp_path, monkeypatch):
    shutil.copy(PROJECT_ROOT / "config.toml", tmp_path / "config.toml")
    (tmp_path / "config.local.toml").write_text('[server]\nport = 8450\nexpose = ["tailscale"]\n')
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    cfg = C.load_config()
    assert bind_specs(cfg) == ["loopback", "tailscale"] and cfg["server"]["port"] == 8450
    assert [os.path.basename(f) for f in cfg["_files"]] == ["config.toml", "config.local.toml"]


def test_example_local_config_is_valid(tmp_path, monkeypatch):
    shutil.copy(PROJECT_ROOT / "config.toml", tmp_path / "config.toml")
    shutil.copy(PROJECT_ROOT / "config.local.example.toml", tmp_path / "config.local.toml")
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    assert bind_specs(C.load_config()) == ["loopback"]


def test_bad_toml_names_the_file(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("[server\n")
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    with pytest.raises(ValueError, match="config.toml"):
        C.load_config()


def test_0_1_database_and_baseline_restore_without_relearning(tmp_path, idle_rows):
    """A data/ dir written by 0.1.x (baseline key "rssi", no caps lines) is picked up by 0.2 with a
    caps-announcing sampler: same baseline, no new learning period, events intact."""
    import time
    rows = idle_rows[:900]
    shift = time.time() - 600 - rows[0]["t"]
    eng = make_engine(tmp_path)
    feed(eng, rows, shift)                      # learns, like the old engine did (no caps line)
    assert eng.detector.state == "clear"
    eng.storage.create_event({"start": 1.0, "end": 2.0, "peak_score": 4.0, "threshold": 3.0, "sources": ["mac"],
                              "armed": False, "label": None, "note": "", "walktest": False})
    asyncio.run(eng.stop())
    eng2 = make_engine(tmp_path)
    t = rows[-1]["t"] + shift + 5
    eng2.on_sample("mac", "mac", {"type": "caps", "link": "up", "per_antenna": "ok", "chains": 2, "t": t}, t, None)
    feed(eng2, idle_rows[900:1000], t + 0.1 - idle_rows[900]["t"])
    assert eng2.detector.state == "clear"       # baseline restored under the same key
    assert len(eng2.storage.list_events()) == 1
    asyncio.run(eng2.stop())


# ---------------------------------------------------------------- address discovery (stubbed)
def test_tailscale_from_cli(monkeypatch):
    monkeypatch.setattr(net, "find_tailscale", lambda: "/x/tailscale")
    monkeypatch.setattr(net, "_run", lambda cmd, timeout=4.0: "100.64.88.2\n" if cmd[1:] == ["ip", "-4"] else None)
    assert net.resolve_spec("tailscale") == "100.64.88.2"
    monkeypatch.setattr(net, "_run", lambda cmd, timeout=4.0: None)   # installed but not running
    assert net.resolve_spec("tailscale") is None
    monkeypatch.setattr(net, "_run", lambda cmd, timeout=4.0: "8.8.8.8\n")
    assert net.resolve_spec("tailscale") is None                       # not a tailnet address


def test_tailscale_cli_locations_include_the_app_bundle():
    assert "/Applications/Tailscale.app/Contents/MacOS/Tailscale" in net.TAILSCALE_PATHS


def test_tailscale_without_cli_uses_a_single_utun_address(monkeypatch):
    monkeypatch.setattr(net, "find_tailscale", lambda: None)
    monkeypatch.setattr(net, "ifconfig_ipv4", lambda: [("lo0", "127.0.0.1"), ("en0", "192.168.1.5"),
                                                       ("utun4", "100.64.70.9")])
    assert net.tailscale_ipv4() == "100.64.70.9"
    monkeypatch.setattr(net, "ifconfig_ipv4", lambda: [("utun4", "100.64.70.9"), ("utun5", "100.64.71.1")])
    assert net.tailscale_ipv4() is None                                # ambiguous: don't guess


def test_lan_address_from_the_sampler(tmp_path):
    (tmp_path / "sensor").mkdir()
    exe = tmp_path / "sensor" / "wifisense"
    exe.write_text('#!/bin/sh\necho \'{"iface":"en7","ipv4":"192.0.2.44","link":"up"}\'\n')
    exe.chmod(0o755)
    assert net.wifi_info(tmp_path) == {"iface": "en7", "ipv4": "192.0.2.44", "link": "up"}
    assert net.resolve_spec("lan", tmp_path) == "192.0.2.44"


def test_ifconfig_parser(monkeypatch):
    out = ("lo0: flags=8049<UP>\n\tinet 127.0.0.1 netmask 0xff000000\n"
           "utun3: flags=8051<UP>\n\tinet 100.64.5.6 --> 100.64.5.6 netmask 0xffffffff\n")
    monkeypatch.setattr(net, "_run", lambda cmd, timeout=4.0: out)
    assert net.ifconfig_ipv4() == [("lo0", "127.0.0.1"), ("utun3", "100.64.5.6")]


def test_unavailable_address_is_retried_until_it_appears(tmp_path):
    """Tailscale not up at login: the spec resolves later and is then bound (here to ::1)."""
    async def run():
        eng = make_engine(tmp_path)
        eng.REBIND_EVERY_S = 0.1
        answers = iter([None, None, "::1"])
        eng.resolve_spec = lambda spec, root: "127.0.0.1" if spec == "loopback" else next(answers, "::1")
        try:
            bound = await eng.serve_http(["loopback", "tailscale"], 0)
            assert bound == ["127.0.0.1:0"] and eng.bind_state["tailscale"]["error"] == "no address yet"
            deadline = time.monotonic() + 20
            while len(eng.bound) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert eng.bound == ["127.0.0.1:0", "::1:0"]
            assert eng.bind_state["tailscale"] == {"spec": "tailscale", "address": "::1", "bound": True, "error": None}
        finally:
            await eng.stop()
    asyncio.run(run())


def test_a_spec_resolving_to_a_wildcard_is_refused(tmp_path):
    async def run():
        eng = make_engine(tmp_path)
        eng.resolve_spec = lambda spec, root: "0.0.0.0"
        try:
            assert await eng.serve_http(["lan"], 0) == []
            assert eng.bind_state["lan"]["error"] == "wildcard refused" and eng.rebind_task is None
        finally:
            await eng.stop()
    asyncio.run(run())


def test_esp32_listener_is_off_by_default(tmp_path):
    async def run():
        eng = make_engine(tmp_path)
        eng.start_sources = True
        eng.cfg["mac"]["enabled"] = False
        await eng.start()
        try:
            assert eng.udp is None and eng.esp32_bound is None
        finally:
            await eng.stop()
    asyncio.run(run())


def test_esp32_listener_binds_the_resolved_lan_address(tmp_path):
    async def run():
        eng = make_engine(tmp_path, esp32={"enabled": True, "bind": "lan", "port": 0})
        eng.start_sources = True
        eng.cfg["mac"]["enabled"] = False
        eng.resolve_spec = lambda spec, root: "127.0.0.1" if spec == "lan" else None
        await eng.start()
        try:
            assert eng.esp32_bound == "127.0.0.1:0" and eng.udp is not None
        finally:
            await eng.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- the real sampler (this Mac only)
@pytest.mark.hardware
def test_real_sampler_probe_shape():
    exe = PROJECT_ROOT / "sensor" / "wifisense"
    if os.environ.get("WIFI_SENTRY_NO_HARDWARE") or sys.platform != "darwin" or not os.access(exe, os.X_OK):
        pytest.skip("no built sampler / not macOS / WIFI_SENTRY_NO_HARDWARE set")
    r = subprocess.run([str(exe), "--probe", "--probe-seconds", "0.5"], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        pytest.skip(f"sampler did not run here: {r.stderr.strip()[:200]}")
    c = json.loads(r.stdout.strip().splitlines()[-1])
    assert c["type"] == "caps" and c["link"] in ("up", "off", "disconnected", "none")
    assert c["per_antenna"] in ("ok", "missing", "unknown") and isinstance(c["chains"], int)
