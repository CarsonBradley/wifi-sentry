# wifi-sentry ESP32 CSI nodes

Small ESP32 boards that measure Wi-Fi **channel state information (CSI)** between your router and
themselves, and stream it to the Mac over UDP. CSI shows how the radio signal on each of ~50-60
sub-carriers is bent and attenuated by the room. A person walking through the room changes it a
lot. RSSI, which the Mac uses, is a single number per antenna; CSI gives about 60 numbers
per packet, 50 times a second.

How it works: each board joins your Wi-Fi and pings the router 50 times a second. Every reply
from the router gives the board one CSI snapshot of the router-to-board radio path, and the
board sends that snapshot to the Mac as one small UDP message. The Mac does the detection.

```
 router ))))  ~ ~ person ~ ~  (((( ESP32 board --UDP 5566--> Mac (wifi-sentry engine)
        <-------- 50 pings/s ---------
```

Contents:

| Path | What |
|---|---|
| `main/` | Firmware (ESP-IDF, C): `main.c` (Wi-Fi, ping, UDP), `csi_encode.c` (JSON encoder), `ws_sign.c` (shared-key signature), `Kconfig.projbuild` (menuconfig settings) |
| `sdkconfig.defaults` | Build settings (CSI on, A-MPDU off, 1 kHz tick). No passwords. |
| `tools/udp_dump.py` | Listen on the Mac, validate the data, and show the rate, loss and an activity meter per board |
| `tools/fake_esp32.py` | Software stand-in for boards: synthetic CSI with scheduled "someone walks by" bursts |
| `test/run_host_test.sh` | Compiles the firmware's JSON encoder on the Mac and validates its worst-case output (`--psa`: also checks the signature code against Python) |

Status: the firmware **compiles** with ESP-IDF v6.1.1 for ESP32-S3, ESP32-C6, ESP32, ESP32-C3 and
ESP32-C5, with zero warnings. It has **not yet run on real hardware**. See "Known unknowns" at
the end.

---

## 1. What to buy

**Recommended: ESP32-S3 boards, 3 of them** (2 to use, 1 spare, or 3 rooms).

- **Seeed Studio XIAO ESP32S3**: about US$8-11. Tiny, USB-C, and has an
  external-antenna connector. Check that the listing includes the antenna; an external antenna
  gives noticeably cleaner CSI than an on-board PCB antenna. Get the plain board, not the
  "Sense" camera version.
- or any **ESP32-S3-DevKitC-1** (official or clone): about US$8-16. A bigger board with a PCB
  antenna. The "-1U" variant needs a separate 2.4 GHz u.FL antenna.
- You also need a **USB-C data cable** (some cheap cables only charge) and a USB phone charger
  per board for permanent power.

Why ESP32-S3:

- On the S3 the firmware records only the *legacy* training field (LLTF), which every Wi-Fi
  frame starts with. Every frame therefore gives exactly the same layout (64 sub-carriers, 128
  numbers), however your router chooses to transmit (802.11n/ax, STBC, ...). That makes the Mac
  side simpler and more robust.
- It is the best-trodden CSI chip: Espressif's esp-csi examples, ESPectre and most published
  hobby work use it. It is dual-core with plenty of memory.

**Alternative: ESP32-C6** (for example Seeed XIAO ESP32C6, about US$5-15). Also
supported, and Espressif rates its raw CSI quality slightly higher. However, on the C6 the CSI
layout follows the frame type the router sends. The firmware turns off Wi-Fi 6 and 40 MHz on the
C6 so you normally get a fixed 57-sub-carrier layout, but some routers will also produce a
double-length (STBC) layout (see section 6). Fine if the S3 is out of stock.

**Not recommended for a first build:** the original ESP32 (works, older and noisier) and the
ESP32-C5 (dual-band and newest, but CSI support is still experimental; the firmware compiles for
it but it is the least tested).

## 2. How many boards and where to put them

The board senses the space **between the router and itself**, plus about a metre around that
line. It is most sensitive when someone crosses that line or walks near the board.

- **One board per room** you care about. For a large open-plan space use **two**, placed so their
  lines to the router cross the space at different angles.
- Put the board **on the far side of the room from the router**, so the room lies between them:
  3-8 m from the router is ideal. Less than about 1 m from the router is bad because the direct
  signal swamps everything.
- Put it at **table or shelf height (0.7-1.5 m)**, roughly the height of a person's torso. Not on
  the floor, and not inside a metal cabinet or behind a TV.
- Keep the router and the boards **where they are** once you start using them. Moving either one
  changes the "empty room" baseline and the Mac has to re-learn it. The same goes for big
  furniture moves.
- Each board needs a **unique node name** (for example `livingroom`, `hallway`).
- If your router is a **mesh system**, a board talks to whichever mesh unit is strongest, and that
  unit is the "router" end of its sensing line.

## 3. Install ESP-IDF on macOS (one time, about 20 minutes, about 6 GB)

ESP-IDF is Espressif's official toolkit for building and flashing these chips. These steps were
tested on the maintainer's Mac (building only) with ESP-IDF **v6.1.1**. Use exactly that version: the CSI API differs between
versions.

1. **Apple command-line tools** (skip this if `git --version` already works):
   ```sh
   xcode-select --install
   ```
2. **Python 3.10 or newer.** macOS ships 3.9, which ESP-IDF v6 rejects. With Homebrew:
   ```sh
   brew install python@3.12
   ```
3. **Download ESP-IDF v6.1.1** (about 0.8 GB). Use `~/esp`, or an external disk if you prefer:
   ```sh
   mkdir -p ~/esp && cd ~/esp
   git clone -b v6.1.1 --recursive --depth 1 --shallow-submodules \
       https://github.com/espressif/esp-idf.git esp-idf-v6.1.1
   ```
4. **Install the compilers** for the chips you bought (about 5 GB, under `~/.espressif`). The
   `PATH=` prefix makes the installer use the new Python:
   ```sh
   cd ~/esp/esp-idf-v6.1.1
   export PATH="$(brew --prefix)/opt/python@3.12/libexec/bin:$PATH"
   ./install.sh esp32s3,esp32c6
   python3 tools/idf_tools.py install cmake ninja
   ```
   (The second command installs CMake and Ninja, which `install.sh` skips on macOS. Running
   `brew install cmake ninja` instead also works.)
5. **Make a shortcut.** Add this line to `~/.zshrc`, then open a new Terminal window:
   ```sh
   alias get_idf='export PATH="$(brew --prefix)/opt/python@3.12/libexec/bin:$PATH"; . ~/esp/esp-idf-v6.1.1/export.sh'
   ```
   From now on, type `get_idf` once in any Terminal window before using `idf.py`. Check it works:
   ```sh
   get_idf
   idf.py --version        # should print: ESP-IDF v6.1.1
   ```

(Espressif also offers a graphical "ESP-IDF Installation Manager" (EIM). It should work too, but
only the steps above were tested.)

## 4. Configure and flash a board

First turn the receiver on: the engine's ESP32 UDP listener is **off by default** because,
without a shared key, the port is unauthenticated (anyone who can reach it can send fake sensor
data). Make up a key, for example with `openssl rand -hex 16`, and put it in `config.local.toml`
(git-ignored), not in `config.toml`:

```toml
[esp32]
enabled = true
bind = "lan"      # listen on this Mac's Wi-Fi address only (default); "0.0.0.0" = every interface
key = "paste-your-own-random-key-here"   # the same string goes into every board
```

With a key set, the engine drops every datagram that isn't signed with it. Leave `key` out (or
`""`) to accept unsigned boards, as before.

then `./wifi-sentry restart`. Find the Mac's current address on your Wi-Fi network (the boards
send to it):

```sh
sensor/wifisense --iface      # {"iface":"en0","ipv4":"192.168.1.23","link":"up"} -> 192.168.1.23
```

**Keep that address stable.** In your router's admin page, create a **DHCP reservation** for the
Mac. Also, in macOS *System Settings > Wi-Fi > (your network) Details*, set *Private Wi-Fi address*
to **Fixed** or **Off**, because a rotating address can get the Mac a new IP. If the IP ever
changes, re-run `menuconfig` with the new address and re-flash every board.

Then, for **each board**:

1. Plug the board into the Mac with the USB-C data cable and find its port:
   ```sh
   ls /dev/cu.usb*          # for example /dev/cu.usbmodem1101
   ```
   If nothing appears, try another cable (charge-only cables are common), or the board's other
   USB-C port if it has two.
2. Select the chip. Do this once per chip type. It **resets the settings**, so do it before step 3:
   ```sh
   get_idf
   cd esp32                             # inside your wifi-sentry folder
   idf.py set-target esp32s3            # or: esp32c6
   ```
3. Enter your settings:
   ```sh
   idf.py menuconfig
   ```
   Use the arrow keys to open **wifi-sentry** and press Enter on each item to edit it:
   - *Wi-Fi network name (SSID)* and *Wi-Fi password*. ESP32-S3 and ESP32-C6 only use
     **2.4 GHz**. If your router has separate 2.4 and 5 GHz names, use the 2.4 GHz one.
   - *Receiver (Mac) IP address*: the address from above (required; there is no default)
   - *Receiver UDP port*: `5566`
   - *Node name*: unique per board, for example `livingroom`
   - *CSI rate*: `50` (pings per second)
   - *Shared key*: the same string as `[esp32] key` on the Mac (empty = unsigned). The board then
     signs every datagram; it doesn't encrypt them.

   Press **S** then Enter to save, and **Q** to quit. The settings, including your password,
   live in `esp32/sdkconfig`, which git ignores. They are never in the source code.
4. Build, flash and watch the log. The first build takes 1-3 minutes:
   ```sh
   idf.py -p /dev/cu.usbmodem1101 flash monitor
   ```
   Within a few seconds you should see lines like:
   ```
   I (812) wifi-sentry: connecting to "MyWiFi"
   I (2950) wifi-sentry: associated: ssid=MyWiFi bssid=aa:bb:cc:dd:ee:ff channel=6
   I (3960) wifi-sentry: got ip 192.168.1.57 gw 192.168.1.1
   I (3961) wifi-sentry: pinging gateway 192.168.1.1 every 20 ms
   I (3962) wifi-sentry: datagrams are signed with the shared key
   I (3965) wifi-sentry: sending CSI to 192.168.1.23:5566 as src "esp32:livingroom"
   I (13000) wifi-sentry: status: online, sent 49.8/s total 450, ...
   ```
   Exit the monitor with **Ctrl-]**. The board keeps running; unplug it and power it from any
   USB charger where you want it.
   - If flashing says *"Failed to connect"*: hold the board's **BOOT** button, tap **RESET** (RST),
     release BOOT, and run the command again.
   - *"SSID not found"* in the log means you used the 5 GHz name or mistyped it. *"wrong
     password?"* means just that. The board retries forever (every 1-30 s) and never needs a
     reboot.
5. **Next board:** run `idf.py menuconfig`, change only the *Node name*, save, then run
   `idf.py -p <port> flash`. Board-to-board this takes under a minute because the build is
   incremental. Switching between S3 and C6 needs `idf.py set-target` again, which means
   re-entering the settings.

## 5. Check that the Mac receives it

```sh
cd esp32                                # inside your wifi-sentry folder
python3 tools/udp_dump.py               # Ctrl-C to stop
```

If macOS asks whether *python3* may accept incoming connections, click **Allow**. Only one
program can listen on port 5566 at a time, so stop the wifi-sentry engine while you use this.
Expected output, every 2 seconds:

```
  20.0s  src                    from                Hz  dev Hz  lost% rssi               ch noise   act%  fmt (len)
        esp32:livingroom       192.168.1.57      49.9    50.0   0.20 -52 (-55..-50)      6   -95    2.1  s3:lltf:n (128)
```

What to look for:

- **Hz is about 50** (or whatever rate you set). Much lower means the router is rate-limiting
  pings or the Wi-Fi is congested. Try a 30 Hz rate.
- **lost% is under about 2.**
- **act%** is a rough motion meter: about 1-5 when the room is still. Walk between the router and
  the board and it should jump (10-40+).
- No `INVALID` lines. `udp_dump.py` checks every message against the format below.
- `python3 tools/udp_dump.py --duration 30` runs for 30 s and prints a summary. Add
  `--save capture.jsonl` to keep the raw data.
- With a shared key, add `--key "<the key>"`: unsigned or wrongly signed datagrams then show up as
  `INVALID` lines (`not signed`, `bad signature`). Without `--key` signatures are not checked.

**Nothing arrives?** Work through these in order:

1. Does the board's log say `sending CSI to <the Mac's IP>`?
2. Is the Mac's IP still the same (`sensor/wifisense --iface`)?
3. Is the listener on? `./wifi-sentry doctor` shows "ESP32 UDP listener enabled on ..." when it is.
4. Is the board on a **guest or IoT network** that is isolated from the Mac?
5. Is the macOS firewall blocking python3? Check *System Settings > Network > Firewall > Options*.
6. Arrives in `udp_dump.py` but not in the engine? Compare the keys (`udp_dump.py --key ...` says
   `bad signature` if they differ), and see "What the engine accepts" below.

## 6. Wire format (for the engine author)

One UDP datagram per CSI frame, one UTF-8 JSON object, no newline, **at most 1400 bytes**. Worst
case is 1328 bytes unsigned and 1369 signed, checked by `test/run_host_test.sh`. A typical S3 frame
is about 450-550 bytes.

```json
{"v":1,"src":"esp32:livingroom","seq":1234,"t_us":81234567,"rssi":-52,"noise":-95,"channel":6,"fmt":"s3:lltf:n","csi":[0,0,0,0,-21,-18, ...]}
{"v":1,"src":"esp32:livingroom","seq":1234,"t_us":81234567,"rssi":-52,"noise":-95,"channel":6,"fmt":"s3:lltf:n","csi":[0,0,0,0,-21,-18, ...],"sig":"<32 lowercase hex digits>"}
```

| key | meaning |
|---|---|
| `v` | protocol version, always 1 |
| `src` | `esp32:` + node name (`[A-Za-z0-9_.-]{1,31}`) |
| `seq` | uint32. +1 for every CSI frame the board accepts. A gap means frames were lost (board queue or network). It restarts at 0 when the board reboots, together with `t_us`. |
| `t_us` | microseconds since board boot, taken in the CSI callback (`esp_timer_get_time()`) |
| `rssi` | dBm of that frame |
| `noise` | RF noise floor, dBm, or `null` when the chip reports something outside -120..-30 |
| `channel` | primary Wi-Fi channel |
| `fmt` | `<chip>:<ltf>:<sec>[:ltf1][:d<k>]`: the layout of `csi` (table below) |
| `csi` | int8 values. Each sub-carrier is a pair **(imaginary, real)**, in ESP-IDF buffer order. |
| `sig` | Only with a shared key: signature, always the **last** member (below). |

**Signature** (menuconfig *Shared key* / `[esp32] key`). The board first builds the datagram
exactly as it would send it unsigned, `U` (bytes, ending in `}`), then sends `U` with its final
`}` replaced by `,"sig":"<hex>"}`, where

- `hex` = the first 16 bytes of **HMAC-SHA256(key, U)**, as 32 lowercase hex digits (truncated
  HMAC, RFC 2104 section 5);
- `key` = the key string's UTF-8 bytes, used as-is (not hex-decoded).

So the signed bytes are the datagram minus its last 41 bytes before the closing brace: a receiver
checks the regular expression `,"sig":"([0-9a-f]{32})"}\Z` on the raw bytes, takes
`U = raw[:match start] + b"}"`, and compares in constant time. Nobody re-serialises JSON. A
signature adds 41 bytes. The firmware computes it with the PSA Crypto API (`main/ws_sign.c`);
`sh test/run_host_test.sh --psa` checks that code against Python's `hmac` for 1-, 16-, 64- and
100-byte keys. The data is **not encrypted**: anyone on the network can still read it.

**What the engine accepts** (`engine/src/sentry/sources.py`):

- Datagrams over 1400 bytes, non-JSON, or with non-numeric fields are dropped and counted.
- At most **16 board names** hold a slot at once. A name that sends nothing accepted for
  **10 minutes** frees its slot. At most 64 different names are accepted per engine run.
- Each name is **pinned to the IP address** that first sent it. Datagrams for that name from
  any other address are dropped (logged once per address) until the name has been silent for
  10 minutes or the engine restarts. A board that gets a new DHCP address therefore drops out for
  up to 10 minutes: give boards DHCP reservations too.
- With `[esp32] key` set: datagrams without a valid `sig` are dropped and counted. Each board's
  `t_us` must also move forward in step with the Mac's clock (2 s of slack). This **replay
  guard** rejects recorded datagrams sent again while the board is live, uptimes that run ahead of
  real time, and a "reboot" (`t_us` going back) unless the board has been silent at least that
  long. A board silent for 30 s, and every board after an engine restart, is trusted again from
  its next signed datagram, so a recording replayed then can still be accepted until the real board
  takes over. Datagrams reordered in transit count as replays too.
- A **channel change** pauses that board's score for `[sources] channel_settle_s` while the CSI
  settles. Only the first 3 changes within 5 minutes do that; more than that and the dashboard
  shows the board's health as "channel keeps changing (possible interference or spoofing)" and
  the board isn't scored until the changes stop for 5 minutes.

**Rules applied on the board** (deterministic; implemented in `main/csi_encode.c`):

1. `csi[0..3]` (the first two sub-carrier slots) are **always 0**. ESP-IDF flags them invalid on
   many frames (`first_word_invalid`, a hardware limitation), so they are always zeroed to keep the
   layout identical frame to frame. In `lltf:n` slot 0 is the DC null anyway; only sub-carrier +1
   is lost.
2. If a buffer would exceed 234 values, two-LTF (STBC) layouts keep only the first LTF (suffix
   `:ltf1`). If it is still too long, the board keeps every k-th sub-carrier, starting at slot 0
   (suffix `:d<k>`). With the firmware's radio settings this should not happen. It is a safety net.
3. `sec` is the secondary-channel field from the radio: `n` none, `a` above, `b` below (`x`
   unknown). The firmware asks for 20 MHz operation, so expect `n`.

**Layouts.** "Slot order" lists which sub-carrier index sits in each consecutive (imag, real)
pair. Indices are taken from the ESP-IDF v6.1.1 docs (*Wi-Fi Vendor Features > Channel State
Information*).

| fmt | chip | csi values | slot order (sub-carrier index) | notes |
|---|---|---|---|---|
| `s3:lltf:n` (also `esp32:`, `c3:`, `s2:`) | ESP32-S3 etc. | **128** (64 sc) | 0..31, then -32..-1 | **What the recommended board sends, always.** Data at +-1..+-26 (pilots at +-7, +-21), nulls at 0, +-27..+-31, -32 |
| `s3:lltf:b` | S3 | 128 | IDF says "0~63" (40 MHz numbering) | Espressif's sample capture shows ascending -32..31 around the primary channel |
| `s3:lltf:a` | S3 | 128 | IDF says "-64~-1" | probably also -32..31 around the primary (not verified) |
| `c6:ht20:n` | ESP32-C6 | **114** (57 sc) | 0..28, then -28..-1 | C6 normal case (11n, 20 MHz). Null at 0 |
| `c6:ht20x2:n` | C6 | 228 | HT-LTF1 (57) then HT-LTF2 (57), each as above | Router used STBC. The first 114 values are directly comparable to `ht20` |
| `c6:lltf:n` | C6 | 106 (53 sc) | 0..26, then -26..-1 | Occasional legacy-rate frames from the router |
| `c6:ht40:n` | C6 | 234 (117 sc) | 0..58, then -58..-1 | Only if the router ignores the 20 MHz request |
| `c6:ht40x2:n:ltf1` | C6 | 234 | as ht40 | reduced from 468 |
| `c6:he20:n:d3` | C6 | 164 (82 sc) | every 3rd slot of 0..122, -122..-1 | only if 11ax gets negotiated (the firmware disables it); reduced from 490 |
| `c5:lltf:n` | ESP32-C5/C61 | 106 (53 sc) | 0..26, then -26..-1 | forced LLTF, 8-bit mode |
| `<chip>:raw<N>:...` | any | N | unknown | length not in the documented table. Pass through, investigate |

Advice for the receiver:

- Use **amplitude** `sqrt(im^2 + re^2)`. Raw phase carries a random per-frame offset and slope
  (carrier and timing offset), so it is useless without sanitisation.
- **Normalise each frame** (for example divide by its mean amplitude). The chip auto-scales CSI
  and its gain control moves, so absolute values drift between frames.
- Drop null sub-carriers: anything that is about 0 over time, including slots 0 and 1.
- Group by `(src, fmt)` and never mix layouts. On a C6 you may see `ht20` and `ht20x2` from the
  same board. Either use only the dominant one, or use the first 114 values of `ht20x2`.
- `act%` in `udp_dump.py` (std/mean of normalised amplitude per sub-carrier, averaged) is a
  reasonable first motion statistic.

## 7. Testing without hardware: `fake_esp32.py`

This emits exactly the format above, using a physical-ish channel model: direct path plus
reflections; a walking person as moving scatterers (phase rotating at walking speed / 12.5 cm
wavelength, different on every sub-carrier) plus body shadowing; random per-frame phase,
AGC jitter, noise, int8 rounding, timing jitter and about 0.5% datagram loss.

```sh
python3 tools/fake_esp32.py                                    # 1 board, idle room, 50 Hz -> 127.0.0.1:5566
python3 tools/fake_esp32.py --motion                           # someone walking continuously
python3 tools/fake_esp32.py --burst-every 30 --burst-len 8     # still for 30 s, walk 8 s, repeat (first walk at t=30 s)
python3 tools/fake_esp32.py --nodes 3 --seed 7 --truth truth.csv   # 3 boards + per-frame ground truth CSV
python3 tools/fake_esp32.py --layout c6 --host 192.168.1.23    # imitate an ESP32-C6, send over the LAN
```

- `--seed N` makes the output deterministic: the same seed and arguments give identical datagrams
  (verified). Only the real-time pacing varies.
- `--truth FILE` writes `src,seq,t_us,motion,sent` for every frame, for scoring a detector.
- `--fast` sends without real-time pacing (for tests); combine it with `--count N` or `--duration S`.
- `--key "<the key>"` signs every datagram like a board with that shared key. Against an engine
  with a key, don't combine it with `--fast` for more than a second or two: the boards' clocks
  then run ahead of real time, and the replay guard drops those frames.
- Other options: `--intensity` (motion strength), `--loss`, `--rate`, `--jitter-ms`, `--channel`, `--name`.

Board-to-board differences are deliberate: each simulated board gets its own distance, multipath,
RSSI and motion coupling. In a 2-board run one board showed act% of about 2 idle and 18-36
walking, while the other, "farther from the action", showed about 1.5 idle and 4-9 walking.

## 8. Known unknowns

- **Not yet run on a real board.** Compiling proves the API usage is right for v6.1.1, not that
  your router behaves. First boot: check Hz, lost% and act% with `udp_dump.py`.
- **Router ping rate limits:** some routers throttle ICMP replies above 20-100/s. If Hz stays well
  below your setting, lower the rate.
- **C6 layouts** are inferred from the ESP-IDF table for the C5 (the C6 page in the v6.1.1 docs has
  an empty CSI section) and from the lengths Espressif's esp-csi tools handle (106/114/228/234/
  468/490). Unknown lengths show up as `raw<N>`, not as garbage.
- The C6/C5 `sec` value comes from the rx_ctrl `second` field, whose exact encoding is not
  documented. With 20 MHz operation it should read 0 (`n`).
- ESP-IDF v6.1.1 was the newest release when this was written. Newer versions may change the CSI
  structs; the firmware prints a compile-time warning on versions older than 6.1.
