"""Regression tests for problems found when the engine, the dashboard, the simulator and the scripts
were first run together (integration pass, 2026-11-23)."""
import asyncio
import gzip
import json
import math
import os
import time

from aiohttp.test_utils import TestClient, TestServer

from sentry.app import create_app
from sentry.detector import Detector
from sentry.features import MacScorer
from sentry.main import acquire_data_lock
from sentry.sim import esp32_stream
from sentry.sources import MAX_ESP32_SOURCES, Esp32Protocol
from sentry.storage import RawRecorder, Storage

from conftest import cfg_with
from test_api import feed, make_engine

ADDR = ("192.0.2.50", 4000)  # TEST-NET-1 documentation address


def _good(src="esp32:hall", **kw):
    m = {"v": 1, "src": src, "seq": 1, "t_us": 1000, "rssi": -50, "noise": -95, "channel": 6,
         "fmt": "s3:lltf:n", "csi": [0, 0, 0, 0] + [10, -12] * 62}
    m.update(kw)
    return json.dumps(m).encode()


# ---------------------------------------------------------------- UDP listener (unauthenticated port)
def test_udp_rejects_garbage_without_reaching_the_detector():
    got = []
    p = Esp32Protocol(lambda *a: got.append(a))
    bad = [
        b"", b"\xff\xfe garbage", b"not json", b"[1,2]", b"null", b'{"v":1}', b"[" * 900 + b"]" * 400,
        b"x" * 5000,                                               # oversized
        _good(csi=["a"] * 128), _good(csi=[[1, 2]] * 64), _good(csi=[True] * 128), _good(csi=[1e300] * 20),
        _good(csi=[1] * 5000)[:1400],                              # truncated JSON
        _good(rssi="loud"), _good(seq="x"), _good(seq=2 ** 70), _good(channel=[1, 2]), _good(fmt={"x": 1}),
        _good(src="esp32:" + "x" * 40), _good(src="esp32:<img src=x>"), _good(src="mac"), _good(src=7),
        b'{"v":1,"src":"esp32:a","rssi":NaN,"csi":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}',
        b'{"v":1,"src":"esp32:a","rssi":1e999,"csi":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}',
        b'{"v":1,"src":"esp32:a","rssi":-Infinity,"csi":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}',
    ]
    for b in bad:
        p.datagram_received(b, ADDR)  # must never raise
    assert got == [] and p.invalid == len(bad) and p.received == 0
    p.datagram_received(_good(), ADDR)
    assert len(got) == 1 and got[0][0] == "esp32:hall" and p.received == 1


def test_udp_caps_the_number_of_sources():
    got = []
    p = Esp32Protocol(lambda src, *a: got.append(src))
    for k in range(MAX_ESP32_SOURCES + 10):
        p.datagram_received(_good(src=f"esp32:n{k}"), ADDR)
    assert len(set(got)) == MAX_ESP32_SOURCES and p.refused_sources == 10
    p.datagram_received(_good(src="esp32:n0", seq=2), ADDR)  # known sources keep working
    assert got[-1] == "esp32:n0"


def test_real_firmware_frames_pass_validation():
    got = []
    p = Esp32Protocol(lambda *a: got.append(a))
    for layout in ("s3", "c6"):
        for _, m, _ in list(esp32_stream(0.5, layout=layout, name="lr"))[:20]:
            p.datagram_received(json.dumps(m, separators=(",", ":")).encode(), ADDR)
    assert p.invalid == 0 and len(got) == 40


def test_status_json_stays_finite_after_hostile_udp(tmp_path):
    """Before the fix a datagram with rssi 1e999 put Infinity into /api/status, which browsers
    cannot parse: the dashboard went blank while the engine kept running."""
    eng = make_engine(tmp_path)
    p = Esp32Protocol(eng.on_sample)
    p.datagram_received(b'{"v":1,"src":"esp32:a","rssi":1e999,"csi":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}', ADDR)
    p.datagram_received(_good(rssi=-48), ADDR)
    eng.tick(float(int(time.time())) + 1)
    json.loads(json.dumps(eng.status(), allow_nan=False))  # raises on NaN/inf
    eng.storage.close()


# ---------------------------------------------------------------- single instance
def test_data_dir_lock_refuses_a_second_engine(tmp_path):
    a = acquire_data_lock(tmp_path / "d")
    assert a is not None
    assert acquire_data_lock(tmp_path / "d") is None
    assert (tmp_path / "d" / "engine.lock").read_text() == str(os.getpid())
    a.close()  # released (also released by the kernel when the process dies)
    b = acquire_data_lock(tmp_path / "d")
    assert b is not None
    b.close()


# ---------------------------------------------------------------- API contract additions
def test_gaps_endpoint(tmp_path):
    async def run():
        eng = make_engine(tmp_path)
        now = time.time()
        eng.storage.add_gap(now - 7200, now - 7000, "engine_down")
        eng.storage.add_gap(now - 600, now - 500, "source_offline")
        gid = eng.storage.open_gap(now - 30, "learning")
        c = TestClient(TestServer(create_app(eng)), headers={"Authorization": "Bearer " + eng.token_path.read_text().strip()})
        await c.start_server()
        try:
            g = (await (await c.get(f"/api/gaps?since={now - 3600}")).json())["gaps"]
            assert [x["reason"] for x in g] == ["source_offline", "learning"]
            assert g[-1]["ongoing"] is True and g[-1]["end"] >= now - 1
            assert len((await (await c.get("/api/gaps")).json())["gaps"]) == 3  # default: last 24 h
            assert (await c.get("/api/gaps?since=abc")).status == 400
            eng.storage.close_gap(gid, now)
        finally:
            await c.close()
            await eng.stop()

    asyncio.run(run())


def test_walk_result_idle_median_is_null_when_unknown(tmp_path):
    d = Detector(cfg_with(baseline={"learning_s": 60}))
    d.walktest_start(100.0)
    d.walktest_stop(130.0)
    r = d.walktest_state()["result"]
    assert r["idle_median"] is None and r["idle_p99"] is None


# ---------------------------------------------------------------- Mac source truthfulness
def test_missing_chains_are_reported_as_null_and_pushed(idle_rows):
    """After a macOS update removes per-chain RSSI, status must not keep showing the last values,
    and the health change must be pushed (the dashboard only re-renders sensors on status)."""
    sc = MacScorer(cfg_with())
    sc.add(dict(idle_rows[0]), 100.0)
    assert sc.extra()["rssi_c0"] is not None
    sc.add(dict(idle_rows[1], rssi_c0=None, rssi_c1=None), 100.1)
    assert sc.extra()["rssi_c0"] is None and sc.extra()["rssi_c1"] is None

    d = Detector(cfg_with(baseline={"learning_s": 60}))
    t0 = idle_rows[0]["t"]
    tick = float(int(t0)) + 1
    pushes, first_health = [], None
    for i, r in enumerate(idle_rows[:400]):
        r = dict(r)
        if r["t"] - t0 > 20:
            r["rssi_c0"] = r["rssi_c1"] = None
        while r["t"] >= tick:
            res = d.tick(tick)
            h = d.status(tick)["sources"][0]["health"]
            if h and first_health is None:
                first_health = tick
            if any(k == "status" for k, _ in res.emits):
                pushes.append((tick, h))
            tick += 1
        d.ingest("mac", "mac", r, r["t"])
    assert first_health is not None
    # pushed on the very tick the message appears, not only at a later state change
    assert (first_health, d.status(first_health)["sources"][0]["health"]) in pushes


# ---------------------------------------------------------------- retention
def test_raw_recorder_rotates_compresses_and_caps(tmp_path):
    rec = RawRecorder(tmp_path / "raw", keep_hours=24, max_bytes=50_000)
    line = json.dumps({"t": 0, "rssi_c0": -42, "pad": "x" * 300})
    base = time.mktime((2026, 11, 23, 0, 30, 0, 0, 0, -1))
    for h in range(6):  # 6 hourly files of incompressible-ish data
        for k in range(200):
            rec.write(line.replace('"t": 0', '"t": %d' % k) + os.urandom(20).hex(), base + h * 3600 + k)
        rec.flush()
    rec.wait_idle()  # the background compression of the last rotation (a sleep was racy on slow runners)
    rec.prune()
    files = sorted(p.name for p in (tmp_path / "raw").iterdir())
    total = sum(p.stat().st_size for p in (tmp_path / "raw").iterdir())
    assert files[-1].endswith(".jsonl") and all(f.endswith(".jsonl.gz") for f in files[:-1])
    assert total <= 50_000 + (tmp_path / "raw" / files[-1]).stat().st_size
    assert len(files) < 6  # oldest hours were dropped to honour max_bytes
    # age limit (the current hour's file is never pruned)
    old = tmp_path / "raw" / "raw-20261120-01.jsonl.gz"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 25 * 3600, time.time() - 25 * 3600))
    rec.prune()
    assert not old.exists() and (tmp_path / "raw" / files[-1]).exists()
    rec.close()


def test_raw_recorder_same_hour_twice_keeps_both(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    with gzip.open(d / "raw-20261101-01.jsonl.gz", "wt") as f:
        f.write('{"first": 1}\n')
    (d / "raw-20261101-01.jsonl").write_text('{"second": 2}\n')
    rec = RawRecorder(d, keep_hours=1e6, max_bytes=10 ** 9)
    rec.write('{"now": 3}', time.time())
    rec.wait_idle()
    with gzip.open(d / "raw-20261101-01.jsonl.gz", "rt") as f:
        assert f.read().splitlines() == ['{"first": 1}', '{"second": 2}']
    rec.close()


def test_history_prune_drops_old_rows(tmp_path):
    st = Storage(tmp_path / "s.db")
    now = time.time()
    for t in (now - 9 * 86400, now - 8 * 86400, now - 3600):
        st.add_history(t, 1.0, 3.0, "clear", {"mac": 1.0})
    st.add_gap(now - 9 * 86400, now - 9 * 86400 + 60, "engine_down")
    st.prune(7, now=now)
    assert st.db.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
    assert st.db.execute("SELECT COUNT(*) FROM history_src").fetchone()[0] == 1
    assert st.gaps(now - 30 * 86400, now) == []
    st.close()


def test_engine_prunes_on_its_tick_loop(tmp_path, idle_rows):
    eng = make_engine(tmp_path, storage={"history_keep_days": 1})
    old = time.time() - 3 * 86400
    eng.storage.add_history(old, 1.0, 3.0, "clear", {"mac": 1.0})
    eng.storage.flush_history()
    feed(eng, idle_rows[:30], time.time() - idle_rows[29]["t"])  # first ticks run the hourly prune
    assert eng.storage.db.execute("SELECT COUNT(*) FROM history WHERE t < ?", (old + 1,)).fetchone()[0] == 0
    assert not math.isinf(eng._last_prune)
    eng.storage.close()


# ---------------------------------------------------------------- event peak
class _StubScorer:
    """Feeds exact raw feature values to the detector (baseline median 0.15, scale 0.15)."""
    kind = "mac"
    rssi, aux = -42.0, {"noise": None, "cca": None, "cca_intf": None, "retrans": None}

    def __init__(self):
        self.r = 0.15

    def add(self, s, t): pass
    def settling(self, t): return False
    def health(self, t): return None
    def feature(self, t): return self.r
    def feature_key(self, t): return "rssi"
    def extra(self): return {}


def test_event_peak_includes_the_ticks_before_it_opened():
    """Live 2026-11-23 01:30:30: scores 5.09 then 3.27 opened an event whose peak was stored as 3.27
    (the opening tick only), so the dashboard rated it 'Slight' instead of 'Moderate'."""
    from sentry.baseline import RobustBaseline
    cfg = cfg_with()
    d = Detector(cfg)
    st = d.ingest("mac", "mac", {}, 0.0)
    st.scorer = _StubScorer()
    bl = RobustBaseline(cfg, "mac")
    bl.load_dict({"learned": True, "buf": [0.15] * 100})
    st.baselines["rssi"] = bl
    for i, score in enumerate([0, 0, 5.09, 3.27, 0, 0]):
        t = 100.0 + i
        st.last_seen = t
        st.scorer.r = 0.15 + 0.15 * score
        d.tick(t)
    ev = d.store.last_event()
    assert ev is not None and abs(ev["peak_score"] - 5.09) < 0.01 and ev["start"] == 102.0


def test_config_local_overlay(tmp_path, monkeypatch):
    import sentry.config as c
    (tmp_path / "config.toml").write_text('[alerts.ntfy]\nenabled = false\npriority = "high"\n')
    (tmp_path / "config.local.toml").write_text('[alerts.ntfy]\nenabled = true\nurl = "https://ntfy.example/x"\n')
    monkeypatch.setattr(c, "PROJECT_ROOT", tmp_path)
    cfg = c.load_config()
    assert cfg["alerts"]["ntfy"] == {"enabled": True, "url": "https://ntfy.example/x", "priority": "high", "token": ""}
    # an explicit --config file is used alone (test instances must not pick up this machine's secrets)
    assert c.load_config(tmp_path / "config.toml")["alerts"]["ntfy"]["enabled"] is False


def test_http_rebinds_an_address_that_comes_up_later(tmp_path):
    """An address that can't be bound at start (Tailscale not up yet at login) is retried."""
    import socket

    async def run():
        eng = make_engine(tmp_path)
        eng.REBIND_EVERY_S = 0.2
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        try:
            bound = await eng.serve_http(["127.0.0.1"], port)
            assert bound == [] and eng.rebind_task is not None
            blocker.close()  # the address becomes available
            deadline = time.monotonic() + 20
            while not eng.bound and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert eng.bound == [f"127.0.0.1:{port}"]
            assert eng.rebind_task.done()
        finally:
            blocker.close()
            await eng.stop()

    asyncio.run(run())
