# wifisense (Mac sensor)

Samples Wi-Fi link-layer signals on macOS and prints one JSON object per line
to stdout (flushed per line). No sudo, no location permission, no GUI prompts,
no scans, never touches the association. The Wi-Fi interface is CoreWLAN's
default interface, whatever its name (en0 on laptops, en1 on many desktops).
The measurements below were made on one Mac (Apple silicon, macOS 27.0.1, build
26A434, 5 GHz channel 157, 80 MHz, 802.11ax 2x2); nothing else has been tested.

## Build and run

    ./build.sh                    # swiftc -> ./wifisense (universal arm64 + x86_64 when possible)
    ./build.sh --native           # this Mac's architecture only
    ./wifisense                   # 10 Hz forever
    ./wifisense --hz 20 --duration 600 > out.jsonl
    ./wifisense --probe           # ~2 s: one JSON object saying which signals this Mac provides
    ./wifisense --iface           # {"iface","ipv4","link"} of the Wi-Fi interface
    ./wifisense --version

Needs only the Xcode Command Line Tools (`xcode-select --install`), Swift 5.7 or
later. Deployment target: macOS 11 for arm64; recent Command Line Tools no
longer ship the x86_64 Swift back-deployment libraries, so there the Intel slice
targets macOS 13 (build.sh tries 11, then 13). Every public API used is
available since macOS 10.10 (`CWWiFiClient`, `CWInterface`), the private ones are
looked up at run time. The version is stamped from `engine/pyproject.toml`.
The x86_64 slice has only been built, never run (no Intel Mac or Rosetta test so far).

Options: `--hz N` (default 10, max 50), `--keepalive HZ` (built-in ICMP echo
to the gateway, default 0 = off, max 10), `--gateway IP`, `--duration S`,
`--slow-every S` (IOReport read interval, default 1 s). Diagnostics go to
stderr. Exits 0 on `--duration` or when stdout is closed (EPIPE); otherwise it
never exits on errors. Unavailable values are `null`.

## Capabilities line

Before the first sample (after 1 s of identical readings), and again whenever
the situation changes for 3 s, wifisense prints

    {"type":"caps","v":1,"t":...,"src":"mac","sampler":"0.2.0","os":"27.0.1","arch":"arm64",
     "model":"Mac17,16","iface":"en1","link":"up","per_antenna":"ok","chains":2,"ioreport":true,"mcs":true}

- `link`: `up` (associated), `disconnected` (Wi-Fi on, no network), `off`
  (Wi-Fi turned off), `none` (no Wi-Fi interface).
- `per_antenna`: `ok` (the private `perAntennaRSSI` call exists, returns the
  expected layout and reports `chains` receive chains), `missing` (call absent,
  wrong return type or unknown layout), `unknown` (link not up).
- The engine picks its scoring mode from this: 2 chains as designed; 1 chain
  scored alone ("reduced"); 3-4 chains generalised; none: the smoothed aggregate
  `rssi` ("degraded", detection will be poor). Without a caps line (older
  recordings) 2 chains are assumed.

`--probe` prints the same object plus what it observed: `reads`,
`chain_reads` (reads with a valid per-antenna value), `chain_changes`,
`chains_valid`, `rssi`/`noise`/`txrate`/`mcs_value` (seen non-null),
`ioreport_channels` of `ioreport_wanted`, `ipv4`, `per_antenna_api`.
Private selectors are only called after checking their Objective-C return type,
so a changed signature in a future macOS reads as `missing` instead of crashing.

## Fields

Fast fields (read every tick):

| key | unit | source | notes |
|---|---|---|---|
| `t` | unix s | wall clock | 3 decimals |
| `src` | "mac" | | |
| `rssi` | dBm int | private `CWFInterface.perAntennaRSSI` aggregate, falls back to public `CWInterface.rssiValue` | same quantity as `rssiValue`; heavily smoothed by the driver |
| `rssi_c0`, `rssi_c1` | dBm int | private `CWFInterface.perAntennaRSSI` | **per receive chain; the only fast-moving RSSI.** Best motion signal. `rssi_c1` is null on a 1-chain Mac |
| `rssi_c2`, `rssi_c3` | dBm int | same | only printed on Macs reporting 3-4 chains (never observed) |
| `noise` | dBm int | `CWInterface.noiseMeasurement` | |
| `txrate` | Mbps | `CWInterface.transmitRate` | almost constant (1200) on this link |
| `channel` | int | `CWInterface.wlanChannel` | refreshed once per second |
| `chan_width` | MHz | `wlanChannel` | refreshed once per second |
| `nss` | int | private `CWFInterface.numberOfSpatialStreams` | refreshed once per second |
| `mcs` | int | private `CWFInterface.MCSIndex` | tx rate adaptation index |
| `rtt` | ms | built-in ICMP keepalive | mean of replies received since the previous line; null when keepalive is off or no reply arrived |
| `rx_pkts`, `tx_pkts` | count | `getifaddrs` if_data | cumulative host packet counters, **32-bit, wrap at 2^32** — diff them mod 2^32. Use as a traffic-load covariate |

Slow fields (driver firmware statistics via private libIOReport, read every
`--slow-every` s; the driver itself only refreshes them every ~5 s, SNR every
~10 s, so values repeat between refreshes):

| key | unit | IOReport channel |
|---|---|---|
| `cca` | % busy | Interface <iface> / Average CCA |
| `cca_obss` | % | Interface <iface> / OBSS CCA (other networks) |
| `cca_ibss` | % | Interface <iface> / IBSS CCA (own BSS) |
| `cca_intf` | % | Interface <iface> / Interference CCA (non-Wi-Fi) |
| `snr` | dB | per-peer LQM SNR (~10 s) |
| `rx_beacons` | cumulative | Chip Frames / Rx Beacon |
| `tx_frames`, `tx_retrans` | cumulative | Chip / Tx Data Frames, Tx Retransmissions |
| `rx_frames`, `rx_retry` | cumulative | Chip / Rx Data Frames, Rx Retry bit set |
| `rx_bad_fcs`, `rx_bad_plcp`, `rx_good_plcp` | cumulative | Chip / Rx Error Counters |

Cumulative counters are chip-wide (include other-BSS frames heard), 64-bit.
Diff consecutive *changed* values to get rates.

## Measured behaviour (the tested Mac, 2026-11-23, room still, at night)

Background traffic on the Wi-Fi interface during all tests: 650–1750 rx packets/s (other
services running on that Mac). Reference capture, included in the repo:
`engine/tests/fixtures/mac-idle-20261123-001939.jsonl.gz` (10 min, default settings, 6000 lines, 1 late tick of 201 ms, zero nulls).

Idle noise and refresh, from that 10-minute capture at 10 Hz:

| field | value changes/s | sd over 10 min | mean sd in 1 s windows | range |
|---|---|---|---|---|
| `rssi_c0` | 2.9 | 0.57 dB | 0.35 dB | -44..-39 |
| `rssi_c1` | 2.9 | 0.50 dB | 0.37 dB | -44..-40 |
| `rssi` (aggregate) | 0.04 | 0.49 dB | 0.01 dB | -36..-34 |
| `noise` | 0.21 | 0.57 dB | 0.09 dB | -90..-87 |
| `mcs` | 0.10 | 0.16 | | 10..11 |
| `txrate` | 0.02 | 26 Mbps | | 1080..1200 |
| `cca` | 0.18 (driver refresh every ~5.3 s) | 7.6 % | | 18..56 % |
| `snr` | 0.03 (refresh ~10.7 s) | 2.6 dB | | 34..40 |
| chip counters | 0.19 (refresh ~5.3 s) | | | |

How fast the per-chain RSSI really refreshes: sampling `perAntennaRSSI` at
30 Hz shows the chain pair changing between consecutive 33 ms reads, so the
driver updates it at least that often (likely per received frame); the 1 dB
quantization hides most updates. Changes seen vs sampling rate (30 s run):
30 Hz → 5.5/s, 20 Hz → 4.5/s, 10 Hz → 3.0/s, 5 Hz → 1.6/s, 2 Hz → 0.85/s.
The aggregate `rssi` (= public `rssiValue`) is a smoothed value that changes
only every few seconds to minutes; it is nearly useless for motion. Use the
per-chain values.

Does keepalive traffic speed up refresh? **No, not on this link.** A/B test,
4 cycles of 60 s off / 60 s on with `ping -i 0.1 -s 16` to the gateway,
sampled at 20 Hz (first 3 s of each block discarded):

| | rssi_c0 changes/s | rssi_c1 changes/s | rssi changes/s | rssi_c0 sd |
|---|---|---|---|---|
| keepalive off | 3.92 | 3.16 | 0.27 | 0.82 |
| keepalive on  | 3.67 | 3.21 | 0.09 | 0.66 |

Block-to-block variation (2.3–5.1 changes/s) is larger than any on/off
effect. The existing traffic already keeps the link busy, so 10 extra packets/s is
~1 % more traffic. An earlier single 3-min idle vs 3-min ping comparison
seemed to show the aggregate `rssi` changing 15x more often with ping; the
interleaved A/B showed that was drift, not the ping.

Gateway RTT (ICMP, 10 Hz, 1692 replies, 0 loss): min 2.1, median 3.6,
p90 33, p99 72, max 104 ms, sd 15.7 ms. The heavy tail comes from AP
scheduling / power save and contention, not multipath, so RTT is a poor
motion signal. It is available with `--keepalive` but off by default.

Defaults chosen: `--hz 10` (5 Hz Nyquist covers body motion; going to 20 Hz
buys ~50 % more per-chain changes at twice the airportd load), keepalive off.

## Limits and caveats

- **No CSI.** These are scalar, 1 dB-quantized RSSI readings; expect coarse
  presence/large-motion detection, not fine gestures or breathing.
- `rssi_c0`/`rssi_c1`, `mcs`, `nss` and all IOReport fields come from
  **private** APIs (CoreWiFi `CWFInterface`, libIOReport). They work without
  entitlements on macOS 27.0.1 but can change or vanish in any OS update; the
  code resolves them at runtime and emits `null` instead of crashing.
  The `perAntennaRSSI` blob layout (Int32[13]: version=1, nChains, ?,
  chain0..3, aggregate, ...) was reverse-engineered, not documented.
- SSID/BSSID are redacted (no Location Services permission); not needed.
  The IOReport per-peer group shows the AP as 00:00:00:00:00:00 for the same reason.
- The link was never truly idle during testing: another service on that Mac
  kept ~650–1750 packets/s flowing on the Wi-Fi interface. Whether per-chain RSSI refreshes
  as often on a *silent* link was not tested. If you run this on a quiet link, try `--keepalive 5`.
- Each tick costs a few XPC round trips into `airportd` (perAntennaRSSI ~5 ms,
  MCSIndex ~3 ms, noise/txrate ~0.3 ms wall time). At 10 Hz: wifisense ~1% of
  one core, airportd +~2.7% of one core over its ~0.8% baseline.
- Wi-Fi-down / interface-missing paths are coded (caps `link` off /
  disconnected / none, nulls, re-acquire every 5 s) but were not exercised on
  hardware (Wi-Fi was never switched off during testing); the engine side is tested with
  simulated sampler output.
- Do not add `scanForNetworks` calls: scans go off-channel and stall traffic.
