"""HTTP API contract tests against a real aiohttp server on 127.0.0.1 (random port)."""
import asyncio
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sentry.app import Engine, create_app
from sentry.sim import inject_mac_motion

from conftest import cfg_with

STATES = {"learning", "clear", "motion", "offline"}


def make_engine(tmp_path, web_dir=None, **extra):
    over = {"paths": {"data_dir": str(tmp_path / "data")},
            "server": {"web_dir": str(web_dir or (tmp_path / "no-web"))},
            "baseline": {"learning_s": 60}}
    for k, v in extra.items():
        over.setdefault(k, {}).update(v)
    return Engine(cfg_with(**over), start_sources=False)


def feed(engine, rows, t_shift):
    """Feed rows shifted by t_shift, ticking at whole seconds like the live loop."""
    next_tick = None
    for r in rows:
        t = r["t"] + t_shift
        if next_tick is None:
            next_tick = float(int(t)) + 1
        while t >= next_tick:
            engine.tick(next_tick)
            next_tick += 1
        engine.on_sample("mac", "mac", dict(r, t=t), t, json.dumps(r))
    return next_tick


def scenario(idle_rows):
    """3 min of idle with a 20 s injected walk at +150 s, shifted to end ~now."""
    rows = idle_rows[:1800]
    t0 = rows[0]["t"]
    rows = inject_mac_motion(rows, [(t0 + 150, t0 + 170)], 3.0, seed=1)
    shift = time.time() - 2 - rows[-1]["t"]
    return rows, shift


async def _client(engine):
    client = TestClient(TestServer(create_app(engine)), headers={"Authorization": "Bearer " + engine.token_path.read_text().strip()})
    await client.start_server()
    return client


def check_status(s):
    assert set(s) >= {"state", "armed", "since", "score", "threshold", "sensitivity", "learning_progress",
                      "sources", "uptime_s", "version"}
    assert s["state"] in STATES
    assert isinstance(s["armed"], bool)
    assert isinstance(s["since"], float) and isinstance(s["score"], float)
    assert isinstance(s["threshold"], float)
    assert s["sensitivity"] in ("low", "medium", "high")
    assert 0.0 <= s["learning_progress"] <= 1.0
    assert "learning_eta_s" in s
    if s["state"] == "learning":
        assert isinstance(s["learning_eta_s"], float) and s["learning_eta_s"] >= 0
    else:
        assert s["learning_eta_s"] is None
    for src in s["sources"]:
        assert set(src) >= {"id", "kind", "online", "rate_hz", "last_seen", "score", "rssi"}
        assert src["kind"] in ("mac", "esp32")
        assert isinstance(src["online"], bool)
        assert isinstance(src["score"], float)


def check_event(e):
    assert set(e) >= {"id", "start", "end", "duration_s", "peak_score", "threshold", "sources", "armed", "label",
                      "note"}
    assert isinstance(e["threshold"], float) and e["threshold"] > 0
    assert isinstance(e["id"], int) and isinstance(e["start"], float)
    assert e["end"] is None or isinstance(e["end"], float)
    assert isinstance(e["sources"], list) and isinstance(e["armed"], bool)
    assert e["label"] in (None, "real", "false")
    assert isinstance(e["note"], str)


def test_full_contract(tmp_path, idle_rows):
    async def run():
        eng = make_engine(tmp_path)
        c = await _client(eng)
        try:
            r = await c.get("/api/status")
            assert r.status == 200
            s = await r.json()
            check_status(s)
            assert s["state"] == "offline"

            r = await c.post("/api/arm", json={"armed": True})
            s = await r.json()
            check_status(s)
            assert s["armed"] is True
            r = await c.post("/api/sensitivity", json={"level": "high"})
            assert (await r.json())["sensitivity"] == "high"
            assert (await c.post("/api/sensitivity", json={"level": "max"})).status == 400
            assert (await c.post("/api/arm", json={"armed": "yes"})).status == 400
            assert (await c.post("/api/arm", data="not json")).status == 400

            rows, shift = scenario(idle_rows)
            feed(eng, rows, shift)
            s = await (await c.get("/api/status")).json()
            check_status(s)
            assert s["state"] in ("clear", "motion")
            assert s["learning_progress"] == 1.0
            assert s["sources"][0]["id"] == "mac" and s["sources"][0]["online"]

            evs = (await (await c.get("/api/events")).json())["events"]
            assert len(evs) >= 1
            for e in evs:
                check_event(e)
            assert [e["id"] for e in evs] == sorted([e["id"] for e in evs], reverse=True)
            ev = evs[-1]
            assert ev["armed"] is True and ev["sources"] == ["mac"]
            assert ev["threshold"] == 2.0  # "high" was in force at the peak
            assert abs(ev["start"] - (rows[0]["t"] + shift + 150)) < 6

            # a later sensitivity change does not re-rate old events
            await c.post("/api/sensitivity", json={"level": "low"})
            old = (await (await c.get("/api/events")).json())["events"][-1]
            assert old["id"] == ev["id"] and old["threshold"] == 2.0
            await c.post("/api/sensitivity", json={"level": "high"})

            # pagination: ids increase monotonically, before=<id> is strictly less
            ids = [e["id"] for e in evs]
            assert ids == sorted(set(ids), reverse=True)
            r = await c.get(f"/api/events?before={evs[0]['id']}")
            assert all(e["id"] < evs[0]["id"] for e in (await r.json())["events"])
            r = await c.get(f"/api/events?limit=1&before={evs[0]['id'] + 1}")
            assert len((await r.json())["events"]) == 1
            r = await c.get(f"/api/events?before={evs[-1]['id']}")
            assert (await r.json())["events"] == []

            # labels
            r = await c.post(f"/api/events/{ev['id']}/label", json={"label": "real", "note": "me, testing"})
            e2 = await r.json()
            check_event(e2)
            assert e2["label"] == "real" and e2["note"] == "me, testing"
            r = await c.post(f"/api/events/{ev['id']}/label", json={"label": None})
            assert (await r.json())["label"] is None
            assert (await r.json())["note"] == "me, testing"
            assert (await c.post(f"/api/events/{ev['id']}/label", json={"label": "maybe"})).status == 400
            assert (await c.post("/api/events/99999/label", json={"label": "real"})).status == 404

            # history
            h = await (await c.get("/api/history?seconds=600&points=60")).json()
            assert set(h) == {"t", "score", "threshold", "per_source", "gaps"}
            learn_gaps = [g for g in h["gaps"] if g["reason"] == "learning"]
            assert len(learn_gaps) == 1 and learn_gaps[0]["end"] - learn_gaps[0]["start"] >= 55
            for g in h["gaps"]:
                assert set(g) >= {"start", "end", "reason"}
                assert g["reason"] in ("engine_down", "source_offline", "learning")
            assert 0 < len(h["t"]) <= 60
            assert len(h["t"]) == len(h["score"]) == len(h["threshold"]) == len(h["per_source"]["mac"])
            assert h["t"] == sorted(h["t"])
            assert max(h["score"]) >= ev["peak_score"] - 1e-3  # max-downsampling keeps the peak
            assert all(th == 2.0 for th in h["threshold"])

            # relearn
            s = await (await c.post("/api/relearn")).json()
            check_status(s)
            assert s["state"] == "learning" or s["learning_progress"] < 1.0

            # cross-origin POST refused, same-origin accepted
            r = await c.post("/api/arm", json={"armed": False}, headers={"Origin": "http://evil.example"})
            assert r.status == 403
            host = f"{c.server.host}:{c.server.port}"
            r = await c.post("/api/arm", json={"armed": False}, headers={"Origin": f"http://{host}"})
            assert r.status == 200 and (await r.json())["armed"] is False
            assert "Access-Control-Allow-Origin" not in r.headers
        finally:
            await c.close()
            await eng.stop()

    asyncio.run(run())


def test_history_max_downsampling(tmp_path):
    eng = make_engine(tmp_path)
    now = time.time()
    for i in range(600):
        eng.storage.add_history(now - 600 + i, float(i % 7), 3.0, "clear", {"mac": float(i % 5)})
    h = eng.storage.history(600, 60, now=now)
    assert len(h["t"]) == 60
    assert set(h["score"]) == {6.0} and set(h["per_source"]["mac"]) == {4.0}  # max within each 10 s bucket
    eng.storage.close()


def test_gaps_persist_across_restart(tmp_path, idle_rows):
    """source_offline gaps are recorded, and a crash + restart leaves an engine_down gap inferred
    from the last persisted heartbeat."""
    async def run():
        rows = idle_rows[:1200]
        shift = time.time() - 400 - rows[0]["t"]
        eng = make_engine(tmp_path)
        nt = feed(eng, rows[:900], shift)          # learning (60 s) then clear
        for k in range(20):                       # 20 s with no samples -> offline
            eng.tick(nt + k)
        feed(eng, rows[1000:1200], (nt + 20) - rows[1000]["t"])  # back online after the gap
        eng.storage.close()                        # simulated crash/kill: no clean stop
        eng2 = make_engine(tmp_path)               # restart: last heartbeat ~270 s ago
        h = eng2.storage.history(3600, 600)
        reasons = [g["reason"] for g in h["gaps"]]
        assert reasons.count("learning") == 1
        assert reasons.count("source_offline") == 1
        off = next(g for g in h["gaps"] if g["reason"] == "source_offline")
        assert 10 <= off["end"] - off["start"] <= 25
        down = [g for g in h["gaps"] if g["reason"] == "engine_down"]
        assert len(down) == 1 and down[0]["end"] - down[0]["start"] > 100
        await eng2.stop()

    asyncio.run(run())


def test_walktest_api(tmp_path, idle_rows):
    async def run():
        eng = make_engine(tmp_path)
        c = await _client(eng)
        try:
            w = await (await c.get("/api/walktest")).json()
            assert w == {"running": False, "started": None, "result": None, "finished": None}
            rows = idle_rows[:1200]
            t0 = rows[0]["t"]
            shift = time.time() - 200 - t0
            nt = feed(eng, rows[:900], shift)  # 90 s: learned (learning_s=60) + some quiet time
            w = await (await c.post("/api/walktest", json={"action": "start"})).json()
            assert w["running"] is True and isinstance(w["started"], float) and w["result"] is None
            assert w["finished"] is None
            n_base = len(eng.detector.sources["mac"].baselines["rssi"].buf)
            walk = inject_mac_motion(rows[900:1200], [(t0 + 92, t0 + 118)], 3.0, seed=3)
            feed(eng, walk, shift)
            assert len(eng.detector.sources["mac"].baselines["rssi"].buf) == n_base  # frozen during the walk
            w = await (await c.post("/api/walktest", json={"action": "stop"})).json()
            assert w["running"] is False
            res = w["result"]
            assert set(res) >= {"duration_s", "peak_score", "median_score", "idle_median", "threshold_by_level",
                                "detected_by_level", "recommended", "message"}
            assert set(res["threshold_by_level"]) == set(res["detected_by_level"]) == {"low", "medium", "high"}
            assert res["recommended"] in ("low", "medium", "high", "none")
            assert res["detected_by_level"]["high"] is True
            assert res["recommended"] != "none"
            assert isinstance(res["message"], str) and res["message"]
            assert (await (await c.get("/api/walktest")).json())["result"]["peak_score"] == res["peak_score"]
            # least sensitive level that RELIABLY caught it
            rel = [lv for lv in ("low", "medium", "high") if res["detected_by_level"][lv]
                   and res["fraction_above_by_level"][lv] >= 0.2]
            assert res["recommended"] == (rel[0] if rel else "none")
            files = sorted((tmp_path / "data" / "walktests").glob("walktest-*.jsonl"))
            assert len(files) == 1
            assert sum(1 for _ in open(files[0])) >= 250
            assert files[0].with_suffix(".result.json").exists()
            assert (await c.post("/api/walktest", json={"action": "dance"})).status == 400
            assert isinstance(w["finished"], float) and w["finished"] >= w["started"]
            # no motion events open during a walk test (so nothing can alert)
            assert (await (await c.get("/api/events")).json())["events"] == []
            assert eng.detector.sources["mac"].baselines["rssi"].n_learn == eng.detector.sources["mac"].baselines["rssi"].learning_ticks
        finally:
            await c.close()
            await eng.stop()

    asyncio.run(run())


def test_walktest_autostop_and_recommendation(tmp_path, idle_rows):
    """Auto-stop after walktest.max_duration_s (180 s); a walk that never reaches threshold -> 'none'."""
    eng = make_engine(tmp_path)
    rows = idle_rows[:3000]
    shift = time.time() - 400 - rows[0]["t"]
    feed(eng, rows[:900], shift)
    eng.walktest("start")
    eng.detector.walk["started"] = rows[900]["t"] + shift  # align to data time
    feed(eng, rows[900:3000], shift)  # 210 s of stillness
    w = eng.detector.walktest_state()
    assert w["running"] is False
    assert 179 <= w["result"]["duration_s"] <= 182
    assert w["result"]["recommended"] == "none"
    assert eng.storage.list_events() == []
    asyncio.run(eng.stop())


def test_sse_stream(tmp_path, idle_rows):
    async def run():
        eng = make_engine(tmp_path)
        c = await _client(eng)
        try:
            resp = await c.get("/api/stream")
            assert resp.headers["Content-Type"].startswith("text/event-stream")

            async def read_events(n, timeout=5.0):
                out, cur = [], {}
                while len(out) < n:
                    try:
                        raw = await asyncio.wait_for(resp.content.readline(), timeout)
                    except asyncio.TimeoutError:
                        break
                    line = raw.decode().rstrip("\n")
                    if line.startswith("event: "):
                        cur["event"] = line[7:]
                    elif line.startswith("data: "):
                        cur["data"] = json.loads(line[6:])
                    elif line == "" and cur:
                        out.append(cur)
                        cur = {}
                return out

            first = await read_events(1)
            assert first[0]["event"] == "status"
            check_status(first[0]["data"])
            # controls push a status to every open stream
            for path, body in (("/api/arm", {"armed": True}), ("/api/sensitivity", {"level": "medium"}),
                               ("/api/relearn", None)):
                r = await c.post(path, json=body) if body is not None else await c.post(path)
                assert r.status == 200
                got = await read_events(1, timeout=2.0)
                assert got and got[0]["event"] == "status"
                check_status(got[0]["data"])
            rows, shift = scenario(idle_rows)
            feed(eng, rows[:1650], shift)  # through the walk at +150..170 s
            seen = {}
            for e in await read_events(1000, timeout=1.0):
                seen.setdefault(e["event"], []).append(e["data"])
            tick = seen["tick"][0]
            assert set(tick) == {"t", "state", "score", "threshold", "sources"}
            assert tick["state"] in STATES and isinstance(tick["sources"], dict)
            assert "status" in seen  # learning -> clear transitions
            learning = [d for d in seen["status"] if d["state"] == "learning"]
            assert len(learning) >= 5  # pushed about every 3 s while learning (60 s here)
            assert learning[0]["learning_eta_s"] > learning[-1]["learning_eta_s"]
            motion = seen.get("motion", [])
            assert motion and motion[0]["end"] is None
            check_event(motion[0])
            resp.close()
        finally:
            await c.close()
            await eng.stop()

    asyncio.run(run())


def test_static_placeholder_and_files(tmp_path):
    async def run():
        eng = make_engine(tmp_path)
        c = await _client(eng)
        try:
            r = await c.get("/")
            assert r.status == 200 and "wifi-sentry" in await r.text()
            assert (await c.get("/api/nope")).status == 404
        finally:
            await c.close()
            await eng.stop()
        web = tmp_path / "web"
        web.mkdir()
        (web / "index.html").write_text("<h1>dash</h1>")
        (web / "app.js").write_text("console.log(1)")
        for name in ("m.json", "i.svg", "i.png", "s.css"):
            (web / name).write_bytes(b"x")
        (tmp_path / "secret.txt").write_text("nope")
        eng = make_engine(tmp_path / "b", web_dir=web)
        c = await _client(eng)
        try:
            assert await (await c.get("/")).text() == "<h1>dash</h1>"
            r = await c.get("/app.js")
            assert await r.text() == "console.log(1)"
            types = {"/app.js": "text/javascript", "/m.json": "application/json", "/i.svg": "image/svg+xml",
                     "/i.png": "image/png", "/s.css": "text/css", "/": "text/html"}
            for path, ct in types.items():
                r = await c.get(path)
                assert r.headers["Content-Type"].startswith(ct), (path, r.headers["Content-Type"])
                assert r.headers["Cache-Control"] == "no-cache"
            assert (await c.get("/../secret.txt")).status == 404
            assert (await c.get("/%2e%2e/secret.txt")).status == 404
        finally:
            await c.close()
            await eng.stop()

    asyncio.run(run())


def test_engine_restart_persistence(tmp_path, idle_rows):
    async def run():
        rows = idle_rows[:900]
        shift = time.time() - 600 - rows[0]["t"]
        eng = make_engine(tmp_path)
        eng.set_sensitivity("low")
        eng.set_armed(True)
        feed(eng, rows, shift)
        assert eng.detector.state == "clear"
        await eng.stop()

        eng2 = make_engine(tmp_path)
        assert eng2.detector.sensitivity == "low" and eng2.detector.armed is True
        feed(eng2, idle_rows[900:1000], shift + 5)
        assert eng2.detector.state == "clear"  # no new learning period
        await eng2.stop()

    asyncio.run(run())


@pytest.mark.parametrize("hosts", [["127.0.0.1", "192.0.2.1", "0.0.0.0"]])
def test_bind_skips_unavailable_and_wildcard(tmp_path, hosts):
    async def run():
        eng = make_engine(tmp_path)
        bound = await eng.serve_http(hosts, 0)
        try:
            assert bound == ["127.0.0.1:0"]
        finally:
            await eng.stop()

    asyncio.run(run())
