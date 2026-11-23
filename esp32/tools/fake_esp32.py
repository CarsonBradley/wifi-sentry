#!/usr/bin/env python3
"""fake_esp32.py - emit synthetic wifi-sentry CSI datagrams exactly like the ESP32 firmware.

Stdlib only, Python 3.9+. Deterministic under --seed: the same arguments produce byte-identical
datagram contents (only the wall-clock pacing differs).

Channel model (per node, per OFDM sub-carrier k at offset f_k = k * 312.5 kHz):
  H_k(t) = g_los(t) * LOS_k + sum(static multipath reflections)_k
           + [moving person: 1-2 scatterers whose path length d(t) changes at walking speed]
  * a person walking rotates the scatterer phase by 2*pi*d(t)/lambda (lambda = 12.5 cm), and the
    delay term makes that fluctuation different on every sub-carrier (frequency-selective fading),
  * the body also shadows the direct path a little (g_los < 1 while moving),
  * then real-receiver impairments: random common phase + timing-offset phase slope per frame
    (so raw phase is useless, as on real hardware), AGC/auto-scale gain jitter, very slow gain
    drift, complex Gaussian noise, rounding to int8, and the firmware rule that values 0..3 are 0.
  Idle frames therefore show only small (~2-4 %) amplitude jitter; walking produces 15-40 %
  amplitude swings that differ across sub-carriers - the signature a CSI motion detector keys on.

Examples:
  python3 tools/fake_esp32.py                                  # 1 node, idle, 50 Hz -> 127.0.0.1:5566
  python3 tools/fake_esp32.py --motion                         # continuous walking
  python3 tools/fake_esp32.py --burst-every 30 --burst-len 8   # quiet 30 s, walk 8 s, repeat
  python3 tools/fake_esp32.py --nodes 3 --layout c6 --seed 7 --truth truth.csv
  python3 tools/fake_esp32.py --key "$KEY"                     # sign like a board with a shared key
"""
import argparse
import cmath
import hashlib
import hmac
import json
import math
import random
import socket
import sys
import time

C = 299_792_458.0
LAMBDA = C / 2.437e9          # channel 6 carrier wavelength, ~12.3 cm
SC_SPACING = 312_500.0        # Hz

# Layouts mirror the firmware's fmt tags (see esp32/README.md, "Wire format").
#   order: the sub-carrier index stored in each (imag, real) slot of the buffer
#   data:  sub-carriers that carry a channel estimate (others are nulls = 0)
LAYOUTS = {
    # ESP32-S3 (and ESP32/C3): LLTF only, secondary channel none -> slots 0..31, -32..-1
    "s3": {"fmt": "s3:lltf:n", "order": list(range(0, 32)) + list(range(-32, 0)), "maxk": 26},
    # ESP32-C6 with 11ax off, HT20: HT-LTF, slots 0..28, -28..-1
    "c6": {"fmt": "c6:ht20:n", "order": list(range(0, 29)) + list(range(-28, 0)), "maxk": 28},
}


class Node:
    def __init__(self, name, layout, rng, channel):
        self.name = name
        self.src = "esp32:" + name
        self.layout = layout
        self.rng = rng
        self.channel = channel
        self.order = layout["order"]
        self.data_sc = [k for k in self.order if k != 0 and abs(k) <= layout["maxk"]]

        r = rng
        # Static environment: direct path + 5 reflections with exponentially decaying power.
        dist = r.uniform(2.0, 8.0)
        self.los_delay = dist / C
        self.paths = []
        for i in range(6):
            if i == 0:
                amp, delay = 1.0, self.los_delay
            else:
                delay = self.los_delay + r.uniform(5e-9, 90e-9)
                amp = r.uniform(0.25, 0.7) * math.exp(-(delay - self.los_delay) / 60e-9)
            self.paths.append((amp, delay, r.uniform(0, 2 * math.pi)))
        self.base_rssi = r.uniform(-62.0, -42.0)
        self.noise_floor = r.randint(-97, -92)
        self.coupling = r.uniform(0.18, 0.38)     # how strongly a walker in the room hits this link
        self.target_amp = r.uniform(14.0, 24.0)   # typical |CSI| after the ESP32 auto-scaling
        self.static = {k: self._static_h(k) for k in self.data_sc}
        self.static_mean = sum(abs(h) for h in self.static.values()) / len(self.static)

        # Moving-person state.
        self.scatterers = []
        self.shadow = 1.0
        self.drift_phase = r.uniform(0, 2 * math.pi)

        self.seq = 0
        self.t_us = r.randint(4_000_000, 9_000_000)   # boot -> first CSI takes a few seconds

    def _static_h(self, k):
        f = k * SC_SPACING
        h = 0j
        for i, (amp, delay, ph) in enumerate(self.paths):
            if i == 0:
                continue
            h += amp * cmath.exp(1j * (ph - 2 * math.pi * f * delay))
        return h

    def _los(self, k):
        amp, delay, ph = self.paths[0]
        return amp * cmath.exp(1j * (ph - 2 * math.pi * k * SC_SPACING * delay))

    def _spawn_scatterers(self):
        r = self.rng
        self.scatterers = []
        for _ in range(r.choice((1, 2, 2))):
            self.scatterers.append({
                "amp": self.coupling * r.uniform(0.6, 1.0),
                "delay": self.los_delay + r.uniform(8e-9, 45e-9),
                "d": r.uniform(0, LAMBDA),          # path-length phase state, metres
                "v": r.uniform(-1.2, 1.2),          # path-length rate, m/s
            })

    def step(self, dt, moving, intensity):
        """Advance the channel by dt seconds and return one datagram dict."""
        r = self.rng
        self.t_us += int(dt * 1e6)
        if moving and not self.scatterers:
            self._spawn_scatterers()
        if not moving and self.scatterers:
            self.scatterers = []
        if moving:
            for s in self.scatterers:
                # Ornstein-Uhlenbeck walking speed: changes direction every second or so.
                s["v"] += -0.8 * s["v"] * dt + 1.1 * math.sqrt(dt) * r.gauss(0, 1)
                s["v"] = max(-1.8, min(1.8, s["v"]))
                s["d"] += s["v"] * dt
                s["delay"] += s["v"] * dt / C
                s["delay"] = max(self.los_delay + 3e-9, min(self.los_delay + 80e-9, s["delay"]))
            target_shadow = max(0.2, 1.0 - 0.35 * intensity * (0.5 + 0.5 * math.sin(self.t_us / 1.7e6)))
            self.shadow += (target_shadow - self.shadow) * min(1.0, dt * 3)
        else:
            self.shadow += (1.0 - self.shadow) * min(1.0, dt * 2)

        # Per-frame receiver impairments.
        cpe = r.uniform(0, 2 * math.pi)                 # carrier phase offset
        sto = r.gauss(0, 0.25)                          # timing offset, in samples (64-pt FFT)
        agc = 10 ** (r.gauss(0, 0.25) / 20)             # AGC / auto-scale jitter (~0.25 dB)
        drift = 1.0 + 0.02 * math.sin(self.t_us / 97e6 + self.drift_phase)
        noise_sigma = 0.55

        hs = {}
        for k in self.data_sc:
            h = self.shadow * self._los(k) + self.static[k]
            f = k * SC_SPACING
            for s in self.scatterers:
                ph = 2 * math.pi * s["d"] / LAMBDA - 2 * math.pi * f * s["delay"]
                h += intensity * s["amp"] * cmath.exp(1j * ph)
            hs[k] = h
        mean_amp = sum(abs(h) for h in hs.values()) / len(hs)
        scale = self.target_amp / self.static_mean * agc * drift

        csi = []
        for k in self.order:
            if k in hs:
                h = hs[k] * scale * cmath.exp(1j * (cpe + 2 * math.pi * k * sto / 64))
                im = h.imag + r.gauss(0, noise_sigma)
                re = h.real + r.gauss(0, noise_sigma)
                csi.append(max(-128, min(127, int(round(im)))))
                csi.append(max(-128, min(127, int(round(re)))))
            else:
                csi.extend((0, 0))
        csi[0:4] = [0, 0, 0, 0]                          # firmware rule: slots 0 and 1 are 0

        rssi = self.base_rssi + 20 * math.log10(max(mean_amp, 1e-6) / self.static_mean) + r.gauss(0, 0.8)
        noise = self.noise_floor + (r.random() < 0.05) * r.choice((-1, 1))
        msg = {
            "v": 1,
            "src": self.src,
            "seq": self.seq & 0xFFFFFFFF,
            "t_us": self.t_us,
            "rssi": int(round(max(-100, min(-10, rssi)))),
            "noise": noise,
            "channel": self.channel,
            "fmt": self.layout["fmt"],
            "csi": csi,
        }
        self.seq += 1
        return msg


def sign(key, data):
    """Append the firmware's signature member: ,"sig":"<first 16 bytes of HMAC-SHA256 as hex>"} replaces
    the closing brace, so the signed bytes are exactly the unsigned datagram (esp32/README.md section 6)."""
    return data[:-1] + b',"sig":"' + hmac.new(key, data, hashlib.sha256).hexdigest()[:32].encode() + b'"}'


def motion_at(t, args):
    if args.motion:
        return True
    if args.burst_every > 0:
        if t < args.burst_every:
            return False
        return (t % args.burst_every) < args.burst_len
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="receiver address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=5566, help="receiver UDP port (default 5566)")
    ap.add_argument("--name", default="fake", help="node name; with --nodes N names are <name>1..<name>N")
    ap.add_argument("--nodes", type=int, default=1, help="number of simulated boards (default 1)")
    ap.add_argument("--layout", choices=sorted(LAYOUTS), default="s3", help="CSI layout to imitate (default s3)")
    ap.add_argument("--rate", type=float, default=50.0, help="frames per second per node (default 50)")
    ap.add_argument("--channel", type=int, default=6, help="Wi-Fi channel to report (default 6)")
    ap.add_argument("--seed", type=int, default=1, help="RNG seed; same seed = same data (default 1)")
    ap.add_argument("--motion", action="store_true", help="someone walks around the whole time")
    ap.add_argument("--burst-every", type=float, default=0.0, metavar="S",
                    help="start a walking burst every S seconds (first one at t=S)")
    ap.add_argument("--burst-len", type=float, default=5.0, metavar="S", help="burst length in seconds (default 5)")
    ap.add_argument("--intensity", type=float, default=1.0, help="motion strength multiplier (default 1.0)")
    ap.add_argument("--loss", type=float, default=0.005,
                    help="fraction of datagrams dropped after seq is assigned, like Wi-Fi/UDP loss (default 0.005)")
    ap.add_argument("--jitter-ms", type=float, default=1.5, help="std-dev of frame timing jitter (default 1.5 ms)")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after S seconds of simulated time (0 = forever)")
    ap.add_argument("--count", type=int, default=0, help="stop after N frames per node (0 = unlimited)")
    ap.add_argument("--fast", action="store_true", help="don't pace in real time (for tests); still deterministic")
    ap.add_argument("--truth", metavar="CSV", help="write ground truth per frame: src,seq,t_us,motion,sent")
    ap.add_argument("--quiet", action="store_true", help="no progress output on stderr")
    ap.add_argument("--key", default="", help="shared key: sign every datagram like a board built with one")
    args = ap.parse_args()

    if args.burst_every and args.burst_len >= args.burst_every:
        ap.error("--burst-len must be shorter than --burst-every")
    if args.rate <= 0 or args.nodes < 1:
        ap.error("--rate must be > 0 and --nodes >= 1")

    master = random.Random(args.seed)
    nodes = []
    for i in range(args.nodes):
        name = args.name if args.nodes == 1 else "%s%d" % (args.name, i + 1)
        nodes.append(Node(name, LAYOUTS[args.layout], random.Random(master.getrandbits(64)), args.channel))
    timing_rng = random.Random(master.getrandbits(64))
    loss_rng = random.Random(master.getrandbits(64))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)
    truth = open(args.truth, "w") if args.truth else None
    if truth:
        truth.write("src,seq,t_us,motion,sent\n")

    def log(msg):
        if not args.quiet:
            print(msg, file=sys.stderr, flush=True)

    period = 1.0 / args.rate
    # Each node runs its own frame clock; we always emit the node whose next frame is earliest.
    next_t = [timing_rng.uniform(0, period) for _ in nodes]
    sent = dropped = 0
    last_motion = None
    wall0 = time.monotonic()
    log("fake_esp32: %d node(s) %s, layout %s, %.0f Hz -> %s:%d, seed %d%s" % (
        len(nodes), ",".join(n.src for n in nodes), args.layout, args.rate, args.host, args.port, args.seed,
        ", signed" if args.key else ""))
    try:
        while True:
            i = min(range(len(nodes)), key=lambda j: next_t[j])
            t = next_t[i]
            if args.duration and t >= args.duration:
                break
            if args.count and all(n.seq >= args.count for n in nodes):
                break
            node = nodes[i]
            dt = period + timing_rng.gauss(0, args.jitter_ms / 1000.0)
            dt = max(0.001, dt)
            next_t[i] = t + dt

            moving = motion_at(t, args)
            if moving != last_motion:
                log("t=%8.2fs  %s" % (t, "MOTION START" if moving else "quiet"))
                last_motion = moving
            if args.count and node.seq >= args.count:
                continue
            msg = node.step(dt if node.seq else 0.0, moving, args.intensity)
            data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
            if args.key:
                data = sign(args.key.encode("utf-8"), data)
            assert len(data) <= 1400, len(data)
            keep = loss_rng.random() >= args.loss
            if truth:
                truth.write("%s,%d,%d,%d,%d\n" % (msg["src"], msg["seq"], msg["t_us"], int(moving), int(keep)))
            if not keep:
                dropped += 1
                continue
            if not args.fast:
                delay = t - (time.monotonic() - wall0)
                if delay > 0:
                    time.sleep(delay)
            elif sent % 200 == 199:
                time.sleep(0.002)          # let a local receiver drain its socket buffer
            sock.sendto(data, dest)
            sent += 1
    except KeyboardInterrupt:
        pass
    finally:
        if truth:
            truth.close()
    log("fake_esp32: sent %d datagrams, dropped %d (simulated loss)" % (sent, dropped))


if __name__ == "__main__":
    main()
