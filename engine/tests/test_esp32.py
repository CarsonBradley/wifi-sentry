"""ESP32 path: the real UDP listener fed by esp32/tools/fake_esp32.py, scored against --truth."""
import asyncio
import json
import subprocess
import sys

import pytest

from sentry.config import PROJECT_ROOT
from sentry.features import Esp32Scorer
from sentry.replay import classify, run_replay, truth_bursts
from sentry.sim import esp32_stream, score_events, truth_intervals
from sentry.sources import start_esp32

from conftest import cfg_with

FAKE = PROJECT_ROOT / "esp32" / "tools" / "fake_esp32.py"


async def _capture(args: list[str]) -> list[dict]:
    got = []
    transport, proto = await start_esp32("127.0.0.1", 0, lambda src, kind, p, t, raw: got.append(p))
    port = transport.get_extra_info("sockname")[1]
    proc = await asyncio.create_subprocess_exec(sys.executable, str(FAKE), "--port", str(port), "--fast", "--quiet",
                                                *args)
    await proc.wait()
    # Let the event loop drain the socket buffer: wait until nothing new arrives for 0.5 s (a fixed
    # sleep was marginal on a loaded runner).
    n, quiet = -1, 0
    for _ in range(200):
        await asyncio.sleep(0.1)
        quiet = quiet + 1 if len(got) == n else 0
        n = len(got)
        if quiet >= 5:
            break
    transport.close()
    assert proto.invalid == 0
    return got


@pytest.fixture(scope="module")
def udp_run(tmp_path_factory):
    truth = tmp_path_factory.mktemp("esp") / "truth.csv"
    msgs = asyncio.run(_capture(["--nodes", "2", "--seed", "11", "--duration", "600", "--burst-every", "60",
                                 "--burst-len", "8", "--truth", str(truth)]))
    for m in msgs:  # replay on the boards' own clocks (t_us), as --fast compresses arrival times
        m.pop("rx_t", None)
    return msgs, str(truth)


def test_udp_simulator_vs_truth(udp_run):
    msgs, truth = udp_run
    assert len(msgs) > 50_000
    samples = classify(msgs, esp_t0=1_000_000.0)
    bursts = truth_bursts(samples, truth)
    assert len(bursts) == 9  # bursts at 60, 120, ... 540 s
    cfg = cfg_with(baseline={"learning_s": 45})
    for level in ("low", "medium", "high"):
        res = run_replay(samples, cfg, sensitivity=level)
        sc = score_events(res["events"], bursts)
        assert sc["detection_rate"] == 1.0, (level, sc)
        assert sc["false_alarms"] == 0, (level, sc)
        assert sc["latency_max_s"] <= 4.0, (level, sc)
    srcs = {e for e in res["detector"].sources}
    assert srcs == {"esp32:fake1", "esp32:fake2"}


def test_seq_gaps_and_reboot():
    cfg = cfg_with()
    sc = Esp32Scorer(cfg)
    frames = [m for _, m, _ in esp32_stream(3.0, nodes=1, seed=4, loss=0.0)]
    t = 100.0
    for m in frames[:50]:
        sc.add(m, t)
        t += 0.02
    for m in frames[55:100]:  # 5 lost
        sc.add(m, t)
        t += 0.02
    assert sc.lost == 5
    dup = dict(frames[99])
    sc.add(dup, t)
    assert sc.dups == 1
    reboot = dict(frames[0], seq=0, t_us=1_000_000)  # seq and t_us restart together
    sc.add(reboot, t + 0.02)
    assert sc.reboots == 1
    assert sc.extra()["reboots"] == 1


def test_weak_coupling_inprocess():
    """In-process simulator, half-strength walking, 3 boards: report-level sanity."""
    flags, samples = [], []
    for t, m, moving in esp32_stream(420, burst_every=60, burst_len=8, nodes=3, seed=21, intensity=0.5,
                                     t0=5_000_000.0):
        samples.append((t, m["src"], "esp32", m))
        flags.append((t, moving))
    bursts = truth_intervals(flags)
    res = run_replay(samples, cfg_with(baseline={"learning_s": 45}), sensitivity="medium")
    sc = score_events(res["events"], bursts)
    assert sc["detection_rate"] >= 0.8 and sc["false_alarms"] == 0, sc
