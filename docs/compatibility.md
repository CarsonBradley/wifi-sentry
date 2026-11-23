# Compatibility reports

wifi-sentry reads per-antenna signal strength through a private macOS API that has been checked on
**two** machines so far, both Apple silicon on macOS 27. Whether your Mac exposes it, and how many antennas it reports, is the
biggest open question in this project. This table collects what people have actually seen.

**Add your Mac:** open a [compatibility report](https://github.com/CarsonBradley/wifi-sentry/issues/new?template=compatibility.yml)
issue, or send a pull request adding a row here. Paste the output of `./wifi-sentry probe` (and,
if you ran it for a while, `./wifi-sentry doctor`). Please remove anything you consider private
first; `doctor` prints local paths and network addresses.

## What the columns mean

- **Probe**: the first lines of `./wifi-sentry probe`. "2 antennas, ok" is the tested design;
  "1 antenna" means *Reduced sensing*; "no per-antenna" means *Degraded sensing*.
- **Detects?**: did the walk test (Settings → Walk test) catch you walking past the Mac? "not
  tried" is fine; a probe result alone is already useful.
- **False alarms**: anything you noticed over a night or a day with nobody moving.

## Reports

| Mac model (`hw.model`) | Chip | macOS | Wi-Fi band / width | Probe | Detects? | False alarms | Reported by / date |
|---|---|---|---|---|---|---|---|
| Mac mini (`Mac17,16`) | Apple silicon | 27.0.1 | 5 GHz, ch 157, 80 MHz, 802.11ax 2x2 | 2 antennas, ok; IOReport 13/13 | Yes, patchily: stirring in bed, turning off an alarm, getting up, coming home; mostly missed ~10 min of moving around the room | First night 27 (a radio artifact, now filtered); none unexplained in the following ~5 h | maintainer, 2026-11 |
| MacBook Pro (`Mac17,2`), on battery | Apple M5 | 27.2.1 | 5 GHz, ch 157, 80 MHz, 2 streams | 2 antennas, ok; IOReport 13/13 (clean install from a copy of the repo, 0.3.0; test suite incl. the hardware test passed; learned and ran at 10 Hz in full sensing) | not tried | not evaluated: two short runs (about 5 and 4 minutes) logged one 14 s event, cause unknown | maintainer, 2026-11 |

## Not yet known

- Any MacBook Air, any Intel or older Apple silicon laptop, and any laptop running on battery for a
  whole night (sleep stops sensing; while armed, the next start or wake sends a "was not running"
  alert).
- Any Intel Mac: the sampler's Intel build has been compiled, never run. With recent Command Line
  Tools the Intel half targets macOS 13 or later.
- Any macOS older than 27: the per-antenna call may not exist (the dashboard would then say
  *Degraded sensing*).
- Any 2.4 GHz link, any 160 MHz link, any mesh network.

## Known portability risks

- The per-antenna call (`CWFInterface.perAntennaRSSI`, reached through a private CoreWiFi selector)
  and its data layout were worked out on macOS 27. Older or newer macOS versions may not have it,
  or may lay it out differently; the sampler type-checks it and falls back to *Degraded sensing*
  instead of crashing.
- The driver statistics read through IOReport (channel busy time, retries) may be named differently
  on other Wi-Fi chips. Missing ones only switch off the interference discount; `probe` shows how
  many of the 13 were found.
- Detection thresholds were tuned on one home and one link. On a new Mac, run the walk test before
  trusting the default sensitivity.
- With `expose = ["lan"]` (and for the ESP32 listener), the Mac's Wi-Fi address is looked up at
  start. If the router hands out a new one, restart, or give the Mac a DHCP reservation.
- With the macOS application firewall on, exposing the dashboard may make macOS ask whether Python
  may accept incoming connections. Not yet observed (the tested Macs have the firewall off).
- Only `127.0.0.1` is bound, not `::1`. Browsers fall back to it for `localhost`, but the link the
  CLI prints (`http://127.0.0.1:8450`) is the dependable one.
