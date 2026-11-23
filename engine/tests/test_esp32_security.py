"""ESP32 UDP path hardening (security review M3 b/c/d): slot expiry, sender-IP pinning, the optional
shared-key signature and replay guard, the raw recorder's size cap, and the channel-change settle bound.
Network use: 127.0.0.1 with port 0 only."""
import asyncio
import json
import logging
import os
import sys
import time

from sentry.config import DEFAULTS
from sentry.detector import Detector
from sentry.features import CHANNEL_FLAP_MAX, HEALTH_CHANNEL_FLAP, Esp32Scorer
from sentry.sim import esp32_stream, load_fake_esp32
from sentry.sources import (ESP32_EXPIRE_S, MAX_ESP32_NAMES, MAX_ESP32_SOURCES, REPLAY_RESYNC_S, Esp32Protocol,
                            esp32_sig, split_sig, start_esp32)
from sentry.storage import RawRecorder

from conftest import cfg_with

fake_esp32 = load_fake_esp32()  # also puts esp32/tools on sys.path
import udp_dump  # noqa: E402

FAKE = fake_esp32.__file__
A, B = ("192.0.2.50", 4000), ("192.0.2.66", 4000)  # TEST-NET-1 documentation addresses
KEY = b"0123456789abcdef-test-key"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def dgram(src="esp32:hall", key=None, **kw) -> bytes:
    m = {"v": 1, "src": src, "seq": 1, "t_us": 5_000_000, "rssi": -50, "noise": -95, "channel": 6,
         "fmt": "s3:lltf:n", "csi": [0, 0, 0, 0] + [10, -12] * 62}
    m.update(kw)
    d = json.dumps(m, separators=(",", ":")).encode()
    return fake_esp32.sign(key, d) if key else d


def proto(key=b"", **kw):
    got, gone = [], []
    clock = Clock()
    p = Esp32Protocol(lambda src, kind, m, t, raw: got.append((src, m)), key=key, on_expire=gone.append,
                      clock=clock, **kw)
    return p, got, gone, clock


# ---------------------------------------------------------------- (b) slots: expiry, IP pinning
def test_slots_expire_after_silence():
    p, got, gone, clock = proto()
    for k in range(MAX_ESP32_SOURCES):
        p.datagram_received(dgram(f"esp32:n{k}"), A)
    p.datagram_received(dgram("esp32:late"), A)
    assert p.refused_sources == 1 and len(p.sources) == MAX_ESP32_SOURCES
    clock.t += ESP32_EXPIRE_S - 60
    p.datagram_received(dgram("esp32:n0", seq=2), A)   # n0 keeps talking, the others go quiet
    clock.t += 61
    p.datagram_received(dgram("esp32:late"), A)        # their slots are free again
    assert got[-1][0] == "esp32:late"
    assert p.expired == MAX_ESP32_SOURCES - 1 and "esp32:n0" in p.sources
    assert sorted(gone) == sorted(f"esp32:n{k}" for k in range(1, MAX_ESP32_SOURCES))
    assert p.counters()["sources"] == ["esp32:late", "esp32:n0"]


def test_distinct_names_per_run_are_bounded():
    """Expiry must not let a sender create endless new sources (the detector keeps a scorer per name)."""
    p, got, gone, clock = proto()
    for k in range(MAX_ESP32_NAMES):
        p.datagram_received(dgram(f"esp32:x{k}"), A)
        if (k + 1) % MAX_ESP32_SOURCES == 0:
            clock.t += ESP32_EXPIRE_S + 1
    assert len({s for s, _ in got}) == MAX_ESP32_NAMES and p.refused_sources == 0
    p.datagram_received(dgram("esp32:one-too-many"), A)
    assert p.refused_sources == 1
    p.datagram_received(dgram("esp32:x0"), A)          # a name seen before may come back
    assert got[-1][0] == "esp32:x0"


def test_name_is_pinned_to_its_first_sender(caplog):
    p, got, gone, clock = proto()
    p.datagram_received(dgram(), A)
    with caplog.at_level(logging.WARNING, logger="sentry.sources"):
        for _ in range(5):
            p.datagram_received(dgram(rssi=-30), B)        # spoofer using the board's name
    assert len(got) == 1 and p.wrong_ip == 5 and p.invalid == 5
    assert sum("pinned to 192.0.2.50" in r.message for r in caplog.records) == 1  # logged once
    p.datagram_received(dgram(seq=2), A)               # the real board is unaffected
    assert len(got) == 2
    clock.t += ESP32_EXPIRE_S + 1                      # board silent 10 min (e.g. new DHCP address)
    p.datagram_received(dgram(seq=3), B)
    assert len(got) == 3 and p.sources["esp32:hall"].ip == B[0]


# ---------------------------------------------------------------- (b) shared-key signature
def test_signature_helpers_agree():
    d = dgram()
    s = fake_esp32.sign(KEY, d)
    assert s.endswith(b'"}') and len(s) == len(d) + 41
    assert split_sig(s) == (d, esp32_sig(KEY, d)) and udp_dump.split_sig(s) == split_sig(s)
    assert udp_dump.sign(KEY, d) == esp32_sig(KEY, d)
    assert split_sig(d) is None
    assert udp_dump.validate(s, KEY)[1] == [] and udp_dump.validate(s)[1] == []
    assert udp_dump.validate(d, KEY)[1] == ["not signed (--key given)"]
    assert udp_dump.validate(s, b"other")[1][0].startswith("bad signature")


def test_key_accepts_signed_and_drops_the_rest():
    p, got, gone, clock = proto(key=KEY)
    p.datagram_received(dgram(key=KEY), A)
    assert len(got) == 1 and "sig" not in got[0][1]
    good = dgram(key=KEY, seq=2, t_us=5_020_000)
    flipped = good[:-3] + (b"0" if good[-3:-2] != b"0" else b"1") + good[-2:]
    tampered = good.replace(b'"rssi":-50', b'"rssi":-20')
    sig_first = b'{"sig":"' + split_sig(good)[1] + b'",' + split_sig(good)[0][1:]
    for bad in (dgram(seq=2, t_us=5_020_000),          # unsigned
                dgram(key=b"wrong key", seq=2, t_us=5_020_000),
                flipped, tampered, sig_first,
                good[:-2] + b'X"}'):                   # not hex
        p.datagram_received(bad, A)
    assert len(got) == 1 and p.unsigned == 3 and p.bad_sig == 3 and p.invalid == 6
    p.datagram_received(good, A)
    assert len(got) == 2


def test_without_key_signed_datagrams_are_accepted():
    p, got, gone, clock = proto()
    p.datagram_received(dgram(key=KEY), A)
    p.datagram_received(dgram(seq=2), A)
    assert len(got) == 2 and p.invalid == 0 and "sig" not in got[0][1]


# ---------------------------------------------------------------- (b) replay guard (key set)
def test_replay_guard():
    p, got, gone, clock = proto(key=KEY)
    frames = []
    for i in range(10):                                # 50 Hz, board clock in step with ours
        frames.append(dgram(key=KEY, seq=100 + i, t_us=600_000_000 + i * 20_000))
        p.datagram_received(frames[-1], A)
        clock.t += 0.02
    assert len(got) == 10
    p.datagram_received(frames[3], A)                  # recorded earlier, sent again
    p.datagram_received(dgram(key=KEY, seq=0, t_us=3_000_000), A)  # "rebooted 3 s ago": heard 20 ms ago
    # from a longer earlier boot: t_us 10 s ahead of what the board can have counted by now
    p.datagram_received(dgram(key=KEY, seq=9999, t_us=610_200_000), A)
    assert len(got) == 10 and p.replayed == 3
    p.datagram_received(dgram(key=KEY, seq=110, t_us=600_200_000), A)  # the live board carries on
    assert len(got) == 11
    clock.t += 8.0                                     # a real reboot: 8 s of silence, 6 s of uptime
    p.datagram_received(dgram(key=KEY, seq=0, t_us=6_000_000), A)
    assert len(got) == 12
    clock.t += REPLAY_RESYNC_S + 1                     # after a long silence the next signed one re-anchors
    p.datagram_received(dgram(key=KEY, seq=5, t_us=900_000_000), A)
    assert len(got) == 13 and p.replayed == 3
    p.datagram_received(dgram(key=KEY, seq=6, t_us=None), A)  # signed but without t_us: can't be checked
    assert len(got) == 13 and p.replayed == 4


def test_fake_esp32_key_end_to_end():
    """fake_esp32.py --key -> the real listener with a key: everything accepted; without --key: nothing.
    Real-time pacing: with --fast the boards' clocks would run ahead of ours, which the replay guard
    rejects once they are more than REPLAY_SLACK_S ahead."""
    async def run(fake_key: str):
        got = []
        transport, p = await start_esp32("127.0.0.1", 0, lambda src, kind, m, t, raw: got.append(m), key=KEY.decode())
        port = transport.get_extra_info("sockname")[1]
        args = ["--port", str(port), "--quiet", "--count", "50", "--loss", "0", "--nodes", "2"]
        proc = await asyncio.create_subprocess_exec(sys.executable, FAKE, *args, *(["--key", fake_key] if fake_key else []))
        await proc.wait()
        await asyncio.sleep(0.3)
        transport.close()
        return got, p

    got, p = asyncio.run(run(KEY.decode()))
    assert len(got) == 100 and p.invalid == 0 and p.replayed == 0
    got, p = asyncio.run(run(""))
    assert got == [] and p.unsigned == 100


# ---------------------------------------------------------------- (c) raw recorder
def _disk(d) -> int:
    return sum(f.stat().st_size for f in d.iterdir())


def test_recorder_enforces_max_bytes_on_every_write(tmp_path):
    d = tmp_path / "raw"
    rec = RawRecorder(d, keep_hours=24, max_bytes=20_000)
    line = json.dumps({"src": "esp32:flood", "csi": [5] * 100})   # ~420 bytes
    t = time.time()
    for i in range(500):                               # ~210 kB within one hour
        rec.write(line, t)
        rec.flush()
        if i % 50 == 0:
            assert _disk(d) <= 20_000
    assert _disk(d) <= 20_000 and rec.dropped > 400
    rec.close()


def test_recorder_makes_room_by_deleting_the_oldest(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    for h, age in ((1, 3000), (2, 2000), (3, 1000)):   # three closed hours, 6 kB each, oldest first
        f = d / f"raw-20261101-0{h}.jsonl.gz"
        f.write_bytes(b"x" * 6000)
        mt = time.time() - age
        os.utime(f, (mt, mt))
    rec = RawRecorder(d, keep_hours=24, max_bytes=20_000)
    t = time.time()
    for _ in range(10):
        rec.write("y" * 499, t)                        # 5 kB now: the oldest file has to go
    rec.flush()
    names = sorted(p.name for p in d.iterdir())
    assert "raw-20261101-01.jsonl.gz" not in names and "raw-20261101-03.jsonl.gz" in names
    assert _disk(d) <= 20_000 and rec.dropped == 0
    rec.close()


def test_esp32_frames_are_not_recorded_by_default():
    assert DEFAULTS["record"]["include_esp32"] is False
    assert cfg_with()["record"]["include_esp32"] is False   # the committed config.toml agrees
    assert DEFAULTS["esp32"]["key"] == "" and cfg_with()["esp32"]["key"] == ""


# ---------------------------------------------------------------- (d) channel-change settle bound
def _frames(n=400):
    return [m for _, m, _ in esp32_stream(n / 50, nodes=1, seed=3, loss=0.0)][:n]


def test_one_channel_change_settles_then_scores():
    sc = Esp32Scorer(cfg_with())
    t = 100.0
    for i, m in enumerate(_frames()):
        sc.add(dict(m, channel=6 if i < 100 else 11), t)
        t += 0.02
    assert sc.n_channel_changes == 1 and sc.health(t) is None
    assert not sc.settling(t)                          # 5 s settle after the change at t=102, now t=108


def test_endless_channel_changes_stop_extending_settle_and_show_in_health():
    cfg = cfg_with()
    sc = Esp32Scorer(cfg)
    settle = float(cfg["sources"]["channel_settle_s"])
    t, ch = 100.0, 6
    for i, m in enumerate(_frames(1500)):              # a spoofer flips the channel every second
        if i and i % 50 == 0:
            ch = 17 - ch
        sc.add(dict(m, channel=ch), t)
        t += 0.02
    assert sc.n_channel_changes > CHANNEL_FLAP_MAX + 20
    assert not sc.settling(t)                          # no longer "settling" indefinitely...
    assert sc.health(t) == HEALTH_CHANNEL_FLAP         # ...but reported instead
    assert sc.extra()["channel_changes"] == sc.n_channel_changes
    # the cap bounds the silent part: only the first CHANNEL_FLAP_MAX changes extended the settle window
    assert sc.settle_until <= 100.0 + CHANNEL_FLAP_MAX * 1.0 + settle + 0.1
    later = t + 301                                    # 5 min without a change: no longer reported
    assert sc.health(later) != HEALTH_CHANNEL_FLAP


def test_channel_flapping_is_visible_in_the_source_status():
    d = Detector(cfg_with(baseline={"learning_s": 60}))
    t, ch = 1000.0, 6
    for i, m in enumerate(_frames(600)):
        if i and i % 50 == 0:
            ch = 17 - ch
        d.ingest("esp32:hall", "esp32", dict(m, channel=ch), t)
        t += 0.02
    src = d.status(t)["sources"][0]
    assert src["id"] == "esp32:hall" and src["health"] == HEALTH_CHANNEL_FLAP
