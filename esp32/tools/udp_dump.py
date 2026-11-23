#!/usr/bin/env python3
"""udp_dump.py - listen for wifi-sentry CSI datagrams, validate them, print per-board stats.

Stdlib only, Python 3.9+. Use it to check a freshly flashed ESP32 (or fake_esp32.py):

  python3 tools/udp_dump.py                    # listen on UDP 5566, print a table every 2 s
  python3 tools/udp_dump.py --duration 20      # run 20 s, exit 0 only if frames arrived and all were valid
  python3 tools/udp_dump.py --save run.jsonl   # also save every valid datagram (one JSON per line)
  python3 tools/udp_dump.py --show 3           # also print the first 3 datagrams verbatim
  python3 tools/udp_dump.py --key "$KEY"       # also require a valid "sig" (boards with a shared key)

Columns:
  Hz      frames/s received in the last interval (should match the board's ping rate, default 50)
  lost%   gaps in "seq" (frames lost on the board queue or on the network), cumulative
  rssi    mean (min..max) in the last interval
  act%    motion activity: mean over sub-carriers of std/mean of per-frame-normalised |CSI|.
          Roughly 1-5 when the room is still; walk near the board and it should jump.

Only one program can own the port: stop the motion engine first if it already listens on 5566.
"""
import argparse
import hashlib
import hmac
import json
import math
import re
import socket
import sys
import time

MAX_DATAGRAM = 1400
MAX_CSI = 234
REQUIRED = ("v", "src", "seq", "t_us", "rssi", "noise", "channel", "fmt", "csi")
OPTIONAL = ("sig",)
# "sig" (boards with a shared key): the datagram's last member, ,"sig":"<32 lowercase hex>"}, where
# hex = first 16 bytes of HMAC-SHA256(key, the datagram without that member). README section 6.
SIG_TAIL = re.compile(rb',"sig":"([0-9a-f]{32})"\}\Z')
SRC_RE = re.compile(r"^esp32:[A-Za-z0-9_.-]{1,31}$")
FMT_RE = re.compile(r"^(?P<chip>[a-z0-9]+):(?P<ltf>[a-z0-9]+):(?P<sec>[nabx])(?P<ltf1>:ltf1)?(?::d(?P<dec>[0-9]+))?$")

# CSI buffer length (int8 values) per (chip family, ltf tag), before reductions. See README.
LEGACY_CHIPS = {"esp32", "s2", "s3", "c3"}
HE_LENGTHS = {"lltf": 106, "ht20": 114, "ht20x2": 228, "ht40": 234, "ht40x2": 468, "he20": 490}


def expected_len(fmt):
    """Expected number of csi values for a fmt tag, or None if the tag is unknown."""
    m = FMT_RE.match(fmt)
    if not m:
        return None
    chip, ltf = m.group("chip"), m.group("ltf")
    if ltf.startswith("raw") and ltf[3:].isdigit():
        n = int(ltf[3:])
    elif chip in LEGACY_CHIPS:
        n = 128 if ltf == "lltf" else None
    else:
        n = HE_LENGTHS.get(ltf)
    if n is None:
        return None
    if m.group("ltf1"):
        n //= 2
    if m.group("dec"):
        k = int(m.group("dec"))
        n = 2 * ((n // 2 + k - 1) // k)
    return n


def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def sign(key, unsigned):
    """The "sig" value (ASCII hex bytes) for an unsigned datagram."""
    return hmac.new(key, unsigned, hashlib.sha256).hexdigest()[:32].encode()


def split_sig(raw):
    """(signed bytes, sig) if raw ends with the firmware's sig member, else None."""
    m = SIG_TAIL.search(raw)
    return None if m is None else (raw[:m.start()] + b"}", m.group(1))


def validate(raw, key=None):
    """Return (obj, errors, warnings) for one datagram's bytes. With key (bytes), require a valid sig."""
    errors, warnings = [], []
    if len(raw) > MAX_DATAGRAM:
        errors.append("datagram is %d bytes (> %d)" % (len(raw), MAX_DATAGRAM))
    signed = split_sig(raw)
    if key:
        if signed is None:
            errors.append("not signed (--key given)")
        elif not hmac.compare_digest(sign(key, signed[0]), signed[1]):
            errors.append("bad signature (wrong key, or the datagram was altered)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["not UTF-8"], warnings
    try:
        obj = json.loads(text)
    except ValueError as e:
        return None, ["not JSON: %s" % e], warnings
    if not isinstance(obj, dict):
        return None, ["top level is not an object"], warnings
    for k in REQUIRED:
        if k not in obj:
            errors.append("missing key %r" % k)
    extra = sorted(set(obj) - set(REQUIRED) - set(OPTIONAL))
    if extra:
        warnings.append("unexpected keys %s" % extra)
    if "sig" in obj and signed is None:
        errors.append('sig must be the last member, 32 lowercase hex digits')
    if errors:
        return obj, errors, warnings

    if not (is_int(obj["v"]) and obj["v"] == 1):
        errors.append("v must be 1, got %r" % (obj["v"],))
    if not (isinstance(obj["src"], str) and SRC_RE.match(obj["src"])):
        errors.append("src must look like esp32:<name>, got %r" % (obj["src"],))
    if not (is_int(obj["seq"]) and 0 <= obj["seq"] <= 0xFFFFFFFF):
        errors.append("seq must be a uint32, got %r" % (obj["seq"],))
    if not (is_int(obj["t_us"]) and obj["t_us"] >= 0):
        errors.append("t_us must be a non-negative int, got %r" % (obj["t_us"],))
    if not (is_int(obj["rssi"]) and -127 <= obj["rssi"] <= 0):
        errors.append("rssi must be an int dBm in -127..0, got %r" % (obj["rssi"],))
    if not (obj["noise"] is None or (is_int(obj["noise"]) and -127 <= obj["noise"] <= 0)):
        errors.append("noise must be null or an int dBm in -127..0, got %r" % (obj["noise"],))
    if not (is_int(obj["channel"]) and 1 <= obj["channel"] <= 196):
        errors.append("channel must be an int 1..196, got %r" % (obj["channel"],))
    csi = obj["csi"]
    if not isinstance(csi, list) or not csi:
        errors.append("csi must be a non-empty list")
    else:
        if len(csi) % 2:
            errors.append("csi length %d is odd (must be imag/real pairs)" % len(csi))
        if len(csi) > MAX_CSI:
            errors.append("csi has %d values (> %d)" % (len(csi), MAX_CSI))
        bad = [x for x in csi if not (is_int(x) and -128 <= x <= 127)]
        if bad:
            errors.append("csi has %d non-int8 values, e.g. %r" % (len(bad), bad[0]))
    fmt = obj["fmt"]
    if not (isinstance(fmt, str) and fmt):
        errors.append("fmt must be a non-empty string")
    elif isinstance(csi, list):
        exp = expected_len(fmt)
        if exp is None:
            warnings.append("unknown fmt %r" % fmt)
        elif exp != len(csi):
            errors.append("fmt %r implies %d csi values, got %d" % (fmt, exp, len(csi)))
    return obj, errors, warnings


class SrcStats:
    def __init__(self, addr):
        self.addr = addr
        self.total = 0
        self.lost = 0
        self.dup_or_reorder = 0
        self.resets = 0
        self.last_seq = None
        self.last_t_us = None
        self.fmts = {}
        self.channel = None
        self.noise = None
        self.max_bytes = 0
        self.reset_interval()

    def reset_interval(self):
        self.n = 0
        self.rssi = []
        self.amp_sum = None
        self.amp_sq = None
        self.amp_n = 0
        self.dev_dt = []

    def add(self, obj, nbytes, addr):
        self.addr = addr
        self.total += 1
        self.n += 1
        self.max_bytes = max(self.max_bytes, nbytes)
        seq, t_us = obj["seq"], obj["t_us"]
        if self.last_seq is not None:
            d = (seq - self.last_seq) & 0xFFFFFFFF
            if d == 0 or d > 0x80000000:
                if self.last_t_us is not None and t_us < self.last_t_us - 1_000_000:
                    self.resets += 1           # board rebooted: seq and uptime restarted
                else:
                    self.dup_or_reorder += 1
            elif d > 1 and d < 100_000:
                self.lost += d - 1
            if self.last_t_us is not None and t_us > self.last_t_us:
                self.dev_dt.append(t_us - self.last_t_us)
        self.last_seq, self.last_t_us = seq, t_us
        self.fmts[(obj["fmt"], len(obj["csi"]))] = self.fmts.get((obj["fmt"], len(obj["csi"])), 0) + 1
        self.channel = obj["channel"]
        self.noise = obj["noise"]
        self.rssi.append(obj["rssi"])

        csi = obj["csi"]
        amps = [math.hypot(csi[i], csi[i + 1]) for i in range(0, len(csi) - 1, 2)]
        mean = sum(amps) / len(amps) if amps else 0
        if mean <= 0:
            return
        amps = [a / mean for a in amps]                 # per-frame normalisation removes AGC
        if self.amp_sum is None or len(self.amp_sum) != len(amps):
            self.amp_sum = [0.0] * len(amps)
            self.amp_sq = [0.0] * len(amps)
            self.amp_n = 0
        for i, a in enumerate(amps):
            self.amp_sum[i] += a
            self.amp_sq[i] += a * a
        self.amp_n += 1

    def activity(self):
        if not self.amp_n or self.amp_n < 5:
            return None
        vals = []
        for s, q in zip(self.amp_sum, self.amp_sq):
            m = s / self.amp_n
            if m < 0.2:                                 # null / guard sub-carriers
                continue
            var = max(0.0, q / self.amp_n - m * m)
            vals.append(math.sqrt(var) / m)
        return 100.0 * sum(vals) / len(vals) if vals else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5566, help="UDP port to listen on (default 5566)")
    ap.add_argument("--bind", default="0.0.0.0", help="address to bind (default 0.0.0.0 = all interfaces)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between stat lines (default 2)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="exit after S seconds; exit code 0 only if valid frames arrived and none were invalid")
    ap.add_argument("--save", metavar="JSONL", help="append every valid datagram to this file")
    ap.add_argument("--show", type=int, default=0, metavar="N", help="print the first N datagrams verbatim")
    ap.add_argument("--key", default="", help="shared key (menuconfig / [esp32] key): unsigned or wrongly "
                                              "signed datagrams then count as INVALID")
    args = ap.parse_args()
    key = args.key.encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    except OSError:
        pass
    try:
        sock.bind((args.bind, args.port))
    except OSError as e:
        print("cannot listen on %s:%d: %s\n(is the motion engine or another udp_dump already using it?)"
              % (args.bind, args.port, e), file=sys.stderr)
        return 2
    sock.settimeout(0.25)
    save = open(args.save, "a") if args.save else None

    stats = {}
    invalid = 0
    error_counts = {}
    shown = 0
    start = time.monotonic()
    last_print = start
    print("listening on UDP %s:%d, signatures %s  (Ctrl-C to stop)" % (
        args.bind, args.port, "required" if key else "not checked"), flush=True)
    try:
        while True:
            now = time.monotonic()
            if args.duration and now - start >= args.duration:
                break
            try:
                raw, addr = sock.recvfrom(65535)
            except socket.timeout:
                raw = None
            if raw is not None:
                if shown < args.show:
                    shown += 1
                    print("[%s:%d] %d bytes: %s" % (addr[0], addr[1], len(raw), raw.decode("utf-8", "replace")))
                obj, errs, warns = validate(raw, key)
                for w in warns:
                    key = "warning: " + w
                    if key not in error_counts:
                        print("%s (from %s)" % (key, addr[0]), flush=True)
                    error_counts[key] = error_counts.get(key, 0) + 1
                if errs:
                    invalid += 1
                    for e in errs:
                        key = "INVALID: " + e
                        if key not in error_counts:
                            print("%s (from %s)" % (key, addr[0]), flush=True)
                        error_counts[key] = error_counts.get(key, 0) + 1
                else:
                    st = stats.get(obj["src"])
                    if st is None:
                        st = stats[obj["src"]] = SrcStats(addr[0])
                        print("new source %s from %s, fmt %s, %d csi values" % (
                            obj["src"], addr[0], obj["fmt"], len(obj["csi"])), flush=True)
                    st.add(obj, len(raw), addr[0])
                    if save:
                        save.write(raw.decode("utf-8") + "\n")
            now = time.monotonic()
            if now - last_print >= args.interval:
                span = now - last_print
                last_print = now
                if not stats and not invalid:
                    print("%6.1fs  no datagrams yet" % (now - start), flush=True)
                    continue
                print("%6.1fs  %-22s %-15s %6s %7s %6s %-16s %4s %5s %6s  %s" % (
                    now - start, "src", "from", "Hz", "dev Hz", "lost%", "rssi", "ch", "noise", "act%", "fmt (len)"))
                for src, st in sorted(stats.items()):
                    hz = st.n / span
                    dev_hz = (1e6 * len(st.dev_dt) / sum(st.dev_dt)) if st.dev_dt else 0.0
                    lost_pct = 100.0 * st.lost / max(1, st.lost + st.total)
                    if st.rssi:
                        rs = "%.0f (%d..%d)" % (sum(st.rssi) / len(st.rssi), min(st.rssi), max(st.rssi))
                    else:
                        rs = "-"
                    act = st.activity()
                    fm = ", ".join("%s (%d)" % k for k in sorted(st.fmts))
                    extra = ""
                    if st.dup_or_reorder or st.resets:
                        extra = "  dup/reorder %d, reboots %d" % (st.dup_or_reorder, st.resets)
                    print("        %-22s %-15s %6.1f %7.1f %6.2f %-16s %4s %5s %6s  %s%s" % (
                        src, st.addr, hz, dev_hz, lost_pct, rs, st.channel,
                        "null" if st.noise is None else st.noise,
                        "-" if act is None else "%.1f" % act, fm, extra), flush=True)
                    st.reset_interval()
                if invalid:
                    print("        invalid datagrams so far: %d" % invalid, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if save:
            save.close()

    total = sum(st.total for st in stats.values())
    print("\nsummary: %d valid datagrams from %d source(s), %d invalid" % (total, len(stats), invalid))
    for src, st in sorted(stats.items()):
        print("  %-22s %d frames, %d lost (seq gaps), %d dup/reorder, %d reboots, max %d bytes, fmt %s" % (
            src, st.total, st.lost, st.dup_or_reorder, st.resets, st.max_bytes,
            ", ".join("%s x%d" % (k[0], v) for k, v in sorted(st.fmts.items()))))
    for k, v in sorted(error_counts.items()):
        print("  %s  x%d" % (k, v))
    return 0 if (total > 0 and invalid == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
