"""Helpers behind ./wifi-sentry (the bash CLI does process management; this does the thinking).

  python -m sentry.cli info            key=value lines for the bash script (paths, port, version)
  python -m sentry.cli pid             pid of the engine owning the data directory (exit 1 if none)
  python -m sentry.cli token           print the access token (created if missing)
  python -m sentry.cli url             dashboard links with the token: this Mac, plus every exposed address
  python -m sentry.cli open-url        just the tokenised link for this Mac (./wifi-sentry open)
  python -m sentry.cli status          one-screen status of the running engine
  python -m sentry.cli probe [--json]  what this Mac's Wi-Fi provides (runs wifisense --probe, ~2 s)
  python -m sentry.cli port-check      exit 1 with an explanation if the HTTP port is taken
  python -m sentry.cli doctor          full self-diagnosis
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__ as VERSION
from . import net
from .auth import load_or_create_token
from .config import PROJECT_ROOT, bind_specs, load_config, resolve

LABEL = "com.wifi-sentry.engine"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# ---------------------------------------------------------------- basics
def cfg_paths(cfg: dict) -> dict:
    data = resolve(cfg, cfg["paths"]["data_dir"])
    tok = resolve({"_root": str(data)}, cfg["auth"]["token_file"])
    return {"root": PROJECT_ROOT, "data": data, "log": data / "logs" / "engine.log", "token": tok,
            "lock": data / "engine.lock", "sampler": PROJECT_ROOT / "sensor" / "wifisense"}


def engine_pid(data_dir: Path) -> int | None:
    """Pid of the engine holding <data>/engine.lock, or None. Exact: asks the kernel whether the lock is held."""
    lock = data_dir / "engine.lock"
    if not lock.exists():
        return None
    with open(lock, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            try:
                return int(fh.read().strip() or 0) or None
            except ValueError:
                return None
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return None


def api(cfg: dict, path: str, timeout: float = 2.0):
    """GET /api/<path> on this Mac with the access token (the API needs it on loopback too)."""
    url = f"http://127.0.0.1:{int(cfg['server']['port'])}/api/{path}"
    headers = {}
    try:
        tok = cfg_paths(cfg)["token"].read_text().strip()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    except OSError:
        pass
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def port_owner(port: int) -> str | None:
    out = subprocess.run(["/usr/sbin/lsof", "+c", "0", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True)
    lines = out.stdout.strip().splitlines()[1:]
    if not lines:
        return None
    parts = lines[0].split()
    return f"{parts[0]} (pid {parts[1]})" if len(parts) > 1 else lines[0]


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if the engine could listen there. SO_REUSEADDR like the engine itself, so connections
    still in TIME_WAIT after a stop don't count as "in use"; an active listener still does."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run_probe(sampler: Path, seconds: float = 2.0) -> dict | None:
    if not (sampler.is_file() and os.access(sampler, os.X_OK)):
        return None
    try:
        r = subprocess.run([str(sampler), "--probe", "--probe-seconds", str(seconds)], capture_output=True,
                           text=True, timeout=seconds + 10)
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return None


def describe_caps(c: dict) -> tuple[str, str, str]:
    """(level, mode, sentence) for a caps / probe object. level: ok | reduced | degraded | none."""
    link = c.get("link")
    if link == "none":
        return "none", "none", "No Wi-Fi interface found. wifi-sentry senses motion through this Mac's Wi-Fi, so it can't work here (an Ethernet-only Mac can still use ESP32 boards)."
    if link == "off":
        return "none", "none", "Wi-Fi is turned off. Turn it on and join your network; wifi-sentry needs the Wi-Fi link to sense motion."
    if link == "disconnected":
        return "none", "none", "Wi-Fi is on but not connected to a network. Join your home Wi-Fi; motion is sensed on the link to the router."
    n = c.get("chains") or 0
    if c.get("per_antenna") == "ok" and n >= 2:
        return "ok", "dual" if n == 2 else "multi", f"Full support: {n} per-antenna signal readings (the design wifi-sentry was tuned on{'' if n == 2 else ', generalised to ' + str(n) + ' antennas'})."
    if c.get("per_antenna") == "ok" and n == 1:
        return "reduced", "single", "Reduced: only one per-antenna reading. Motion is scored on it, but radio artifacts that move all antennas together can't be rejected as well; expect more false alarms."
    return "degraded", "aggregate", "Degraded: this Mac/macOS doesn't expose per-antenna signal. wifi-sentry falls back to the overall signal strength, which the driver smooths heavily: detection will be poor. ESP32 boards are the alternative."


# ---------------------------------------------------------------- commands
def cmd_info(cfg, a) -> int:
    p = cfg_paths(cfg)
    print(f"version={VERSION}")
    print(f"root={p['root']}")
    print(f"data={p['data']}")
    print(f"log={p['log']}")
    print(f"port={int(cfg['server']['port'])}")
    print(f"token_file={p['token']}")
    return 0


def cmd_pid(cfg, a) -> int:
    pid = engine_pid(cfg_paths(cfg)["data"])
    if pid is None:
        return 1
    print(pid)
    return 0


def cmd_token(cfg, a) -> int:
    tok, created = load_or_create_token(cfg_paths(cfg)["token"])
    if created:
        print(f"(generated a new access token in {cfg_paths(cfg)['token']})", file=sys.stderr)
    print(tok)
    return 0


def tailscale_dns_name() -> str | None:
    cli = net.find_tailscale()
    if not cli:
        return None
    try:
        r = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=4)
        name = json.loads(r.stdout).get("Self", {}).get("DNSName", "")
        return name.rstrip(".") or None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def cmd_url(cfg, a) -> int:
    port = int(cfg["server"]["port"])
    tok, _ = load_or_create_token(cfg_paths(cfg)["token"])
    dbg = api(cfg, "debug")
    bound = {b.rsplit(":", 1)[0] for b in (dbg or {}).get("bound", [])}
    running = dbg is not None
    print(f"On this Mac:  http://127.0.0.1:{port}/?token={tok}   (or: ./wifi-sentry open)")
    specs = [s for s in bind_specs(cfg) if s not in ("loopback", "localhost", "127.0.0.1")]
    if not specs:
        print("Other devices: not enabled. Add expose = [\"tailscale\"] (or \"lan\") under [server] in "
              "config.local.toml, then ./wifi-sentry restart.")
    for spec in specs:
        addr = net.resolve_spec(spec, PROJECT_ROOT)
        label = {"tailscale": "Tailscale", "lan": "Wi-Fi network"}.get(spec, spec)
        if addr is None:
            print(f"{label}: no address right now ({'Tailscale not running?' if spec == 'tailscale' else 'not connected?'})")
            continue
        host = f"[{addr}]" if ":" in addr else addr
        state = "" if not running else ("" if addr in bound else "   (engine not listening here yet)")
        print(f"{label}:  http://{host}:{port}/?token={tok}{state}")
        if spec == "tailscale":
            dns = tailscale_dns_name()
            if dns:
                print(f"Tailscale MagicDNS:  http://{dns}:{port}/?token={tok}")
    print("Open a link once per browser: it stores a login cookie and drops the token from the address bar.")
    if specs:
        print("Plain HTTP: only for your own network / VPN. Anyone with the token can see and control the dashboard.")
    if not running:
        print("(the engine is not running: ./wifi-sentry start)")
    return 0


def cmd_open_url(cfg, a) -> int:
    """The tokenised local dashboard link (for ./wifi-sentry open)."""
    tok, _ = load_or_create_token(cfg_paths(cfg)["token"])
    print(f"http://127.0.0.1:{int(cfg['server']['port'])}/?token={tok}")
    return 0


def fmt_status(s: dict) -> list[str]:
    out = ["state %s  armed %s  sensitivity %s  score %.2f / threshold %.1f  learning %.0f%%  sensing %s" % (
        s["state"], s["armed"], s["sensitivity"], s["score"], s["threshold"], 100 * s["learning_progress"],
        s.get("sensing", "?"))]
    arm, al = s.get("arm"), s.get("alerts")
    if arm or al:
        why = {"delay": "exit delay running", "schedule": "by schedule", "manual": "set by hand"}.get((arm or {}).get("mode"), "")
        nc = (arm or {}).get("next_change")
        when = time.strftime("%a %H:%M", time.localtime(nc["t"])) if nc else ""
        out.append("arming: " + (why or "?") + (f", {'arms' if nc['armed'] else 'disarms'} {when}" if nc else "")
                   + "  alerts: " + (", ".join((al or {}).get("channels") or []) or "none (dashboard: Phone alerts)")
                   + ("  FAILING: " + "; ".join(f"{f['channel']}: {f['error']}" for f in al["failing"]) if al and al.get("failing") else ""))
    for x in s["sources"]:
        extra = x.get("health") or x.get("notice") or ""
        mode = f" mode={x['signal_mode']}" if x.get("signal_mode") else ""
        out.append("  %-14s %-5s online=%s %5.1f Hz score=%s rssi=%s%s %s" % (
            x["id"], x["kind"], x["online"], x["rate_hz"], x["score"], x["rssi"], mode, extra))
    return out


def cmd_status(cfg, a) -> int:
    p = cfg_paths(cfg)
    pid = engine_pid(p["data"])
    print(f"wifi-sentry {VERSION}")
    if pid is None:
        print("engine: not running")
        return 3
    up = subprocess.run(["ps", "-o", "etime=,%cpu=,rss=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    print(f"engine: running, pid {pid}" + (f", up {up[0]}, cpu {up[1]}%, {int(up[2]) // 1024} MB" if len(up) == 3 else ""))
    s = api(cfg, "status")
    if s is None:
        print("API: not reachable on 127.0.0.1:%d" % int(cfg["server"]["port"]))
        return 1
    for line in fmt_status(s):
        print(line)
    dbg = api(cfg, "debug") or {}
    print("listening: " + ", ".join(f"http://{b}/" for b in dbg.get("bound", [])))
    if dbg.get("esp32_bound"):
        print(f"ESP32 UDP: {dbg['esp32_bound']} ({'signed datagrams only' if cfg['esp32'].get('key') else 'unauthenticated: no [esp32] key'})")
    return 0


def cmd_probe(cfg, a) -> int:
    c = run_probe(cfg_paths(cfg)["sampler"], a.seconds)
    if c is None:
        print("could not run sensor/wifisense --probe (not built? run ./install.sh)", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(c))
        return 0
    level, mode, sentence = describe_caps(c)
    print(f"This Mac: {c.get('model')} ({c.get('arch')}), macOS {c.get('os')}, Wi-Fi interface {c.get('iface') or 'none'}, link {c.get('link')}")
    print(sentence)
    if c.get("link") == "up":
        ch = c.get("chains_valid", c.get("chains"))
        print(f"  per-antenna signal: {c.get('per_antenna')} ({ch} antenna value(s); {c.get('chain_reads', 0)}/{c.get('reads', 0)} reads valid, {c.get('chain_changes', 0)} changes in {a.seconds:g} s)")
        print(f"  overall signal (rssi): {'yes' if c.get('rssi') else 'no'};  noise: {'yes' if c.get('noise') else 'no'};  MCS: {'yes' if c.get('mcs_value') else 'no'}")
        print(f"  driver statistics (IOReport): {c.get('ioreport_channels', 0)} of {c.get('ioreport_wanted', 13)} channels"
              + ("" if c.get("ioreport_channels") else " (only used to discount interference; optional)"))
    return 0 if level in ("ok", "reduced", "degraded") else 2


def cmd_port_check(cfg, a) -> int:
    port = int(cfg["server"]["port"])
    if port_free(port):
        return 0
    who = port_owner(port)
    print(f"Port {port} on 127.0.0.1 is already in use{' by ' + who if who else ''}.\n"
          f"Pick another one: add  [server] port = 8451  (any free port) to config.local.toml, then start again.",
          file=sys.stderr)
    return 1


def cmd_launchagent(cfg, a) -> int:
    """Write the LaunchAgent plist for THIS checkout (paths generated here, never committed)."""
    import plistlib
    p = cfg_paths(cfg)
    path_dirs = [d for d in (a.uv_dir, "/usr/bin", "/bin", "/usr/sbin", "/sbin") if d]
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(PROJECT_ROOT / "run.sh"), "--log-file", str(p["log"])],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(path_dirs))},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # restart after a crash, not after ./wifi-sentry stop
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(p["data"] / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(p["data"] / "logs" / "launchd.err.log"),
    }
    (p["data"] / "logs").mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLIST.with_suffix(".plist.tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(plist, f)
    os.replace(tmp, PLIST)
    print(PLIST)
    return 0


def agent_is_mine() -> bool:
    """True if the installed LaunchAgent runs THIS checkout. Parsed, not grepped: the path may contain
    spaces, '&' (escaped in the XML) or regex characters."""
    import plistlib
    try:
        with open(PLIST, "rb") as f:
            args = plistlib.load(f).get("ProgramArguments") or []
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    return bool(args) and args[0] == str(PROJECT_ROOT / "run.sh")


def cmd_agent_mine(cfg, a) -> int:
    return 0 if agent_is_mine() else 1


# ---------------------------------------------------------------- doctor
class Report:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def line(self, level: str, msg: str) -> None:
        tag = {"ok": "  ok ", "warn": "WARN ", "fail": "FAIL ", "info": "     "}[level]
        if level == "fail":
            self.fails += 1
        elif level == "warn":
            self.warns += 1
        print(f"{tag} {msg}")

    def head(self, title: str) -> None:
        print(f"\n{title}")


def _run_text(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _du(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n < 1e9 else f"{n / 1e9:.2f} GB"


def cmd_doctor(cfg, a) -> int:
    r = Report()
    p = cfg_paths(cfg)
    port = int(cfg["server"]["port"])
    commit = _run_text(["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"])
    print(f"wifi-sentry {VERSION}{' (' + commit + ')' if commit else ''} at {PROJECT_ROOT}")

    r.head("System")
    mac_ver = platform.mac_ver()[0]
    if sys.platform != "darwin":
        r.line("fail", f"not macOS ({sys.platform}); wifi-sentry needs a Mac")
    else:
        major = int(mac_ver.split(".")[0]) if mac_ver else 0
        model = _run_text(["sysctl", "-n", "hw.model"])
        r.line("ok" if major >= 11 else "fail", f"macOS {mac_ver}, {platform.machine()}, {model}"
               + ("" if major >= 11 else " (needs macOS 11 or later)"))
    clt = _run_text(["xcode-select", "-p"])
    swift = _run_text(["swiftc", "--version"]).splitlines()
    r.line("ok" if clt and swift else "warn", f"Command Line Tools: {clt or 'not installed'}"
           + (f", {swift[0][:60]}" if swift else " (only needed to rebuild the sampler: xcode-select --install)"))
    uv = shutil.which("uv") or next((x for x in (os.path.expanduser("~/.local/bin/uv"), "/opt/homebrew/bin/uv") if os.path.exists(x)), None)
    r.line("ok" if uv else "warn", f"uv: {_run_text([uv, '--version']) if uv else 'not found'}; Python {platform.python_version()} ({sys.executable})")

    r.head("Configuration")
    r.line("ok", "files: " + (", ".join(Path(f).name for f in cfg["_files"]) or "built-in defaults only"))
    if not (PROJECT_ROOT / "config.local.toml").exists():
        r.line("info", "no config.local.toml (fine; ./install.sh creates one from config.local.example.toml)")
    for note in cfg["_notes"]:
        r.line("warn", note)

    r.head("Sampler (this Mac's Wi-Fi)")
    sp = p["sampler"]
    caps = None
    if not sp.exists():
        r.line("fail", "sensor/wifisense is not built: run ./install.sh (or sensor/build.sh)")
    else:
        sv = _run_text([str(sp), "--version"])
        archs = _run_text(["lipo", "-archs", str(sp)])
        if not sv:
            r.line("fail", "sensor/wifisense does not run on this Mac (wrong architecture?): rebuild with sensor/build.sh")
        else:
            r.line("ok" if sv == VERSION else "warn", f"sensor/wifisense {sv} [{archs}]"
                   + ("" if sv == VERSION else f" - built from a different version than the engine ({VERSION}): run sensor/build.sh"))
        if cfg["mac"]["enabled"] and sv:
            caps = run_probe(sp, 2.0)
            if caps is None:
                r.line("fail", "wifisense --probe failed")
            else:
                level, mode, sentence = describe_caps(caps)
                r.line({"ok": "ok", "reduced": "warn", "degraded": "warn", "none": "fail"}[level], sentence)
                if caps.get("link") == "up":
                    r.line("info", f"interface {caps.get('iface')}, per-antenna {caps.get('per_antenna')} "
                           f"({caps.get('chains_valid', caps.get('chains'))} values, {caps.get('chain_changes', 0)} changes in 2 s), "
                           f"IOReport {caps.get('ioreport_channels', 0)}/{caps.get('ioreport_wanted', 13)} channels")
        if not cfg["mac"]["enabled"]:
            r.line("info", "[mac] enabled = false: the Mac's own Wi-Fi is not used")
        elif Path(str(cfg["mac"]["command"][0])).name != "wifisense":
            r.line("warn", f"[mac] command runs {' '.join(map(str, cfg['mac']['command']))} instead of sensor/wifisense")

    r.head("Engine")
    pid = engine_pid(p["data"])
    status = api(cfg, "status") if pid else None
    dbg = api(cfg, "debug") if pid else None
    if pid is None:
        r.line("info", "not running (./wifi-sentry start)")
        if port_free(port):
            r.line("ok", f"port {port} is free")
        else:
            who = port_owner(port)
            r.line("fail", f"port {port} is in use{' by ' + who if who else ''}: set [server] port in config.local.toml")
    elif status is None:
        r.line("fail", f"running (pid {pid}) but the API on 127.0.0.1:{port} does not answer; see ./wifi-sentry logs")
    else:
        r.line("ok" if status.get("version") == VERSION else "warn",
               f"running, pid {pid}, version {status.get('version')}, up {status.get('uptime_s', 0) / 60:.0f} min, state {status['state']}"
               + ("" if status.get("version") == VERSION else f" (code on disk is {VERSION}: ./wifi-sentry restart)"))
        for src in status["sources"]:
            if src["kind"] != "mac":
                continue
            rate = src.get("rate_hz") or 0
            expected = 10.0
            r.line("ok" if src["online"] and rate >= 0.8 * expected else "warn",
                   f"Mac sampler: {rate:.1f} samples/s (expected ~{expected:.0f}), signal mode {src.get('signal_mode') or '?'}")
            if src.get("health"):
                r.line("fail", f"Mac sensor can't sense: {src['health']}")
            if src.get("notice"):
                r.line("warn", src["notice"])
            mp = (dbg or {}).get("mac_process") or {}
            if mp.get("restarts"):
                r.line("warn", f"wifisense restarted {mp['restarts']} time(s); last error: {mp.get('last_error')}")
        r.line("info", f"sensing: {status.get('sensing', 'unknown (engine older than 0.2.0)')}; armed {status['armed']}; "
               f"sensitivity {status['sensitivity']}")

    r.head("Network access")
    specs = bind_specs(cfg)
    live = {b["spec"]: b for b in (dbg or {}).get("bind", [])}
    for spec in specs:
        addr = net.resolve_spec(spec, PROJECT_ROOT)
        lb = live.get(spec)
        where = f"{spec} -> {addr or 'no address now'}"
        if spec == "tailscale":
            cli = net.find_tailscale()
            where += f" (tailscale CLI: {cli or 'not found'})"
        if lb is not None:
            r.line("ok" if lb.get("bound") else "warn", where + (": listening" if lb.get("bound") else f": not listening ({lb.get('error')}; retried every 30 s)"))
        else:
            r.line("ok" if addr else "warn", where)
    if any(s not in ("loopback", "localhost", "127.0.0.1") for s in specs):
        r.line("info", "exposed beyond this Mac: the access token is required there (./wifi-sentry url). Plain HTTP: trusted network / VPN only.")
    else:
        r.line("info", "only this Mac can open the dashboard (add expose = [\"tailscale\"] in config.local.toml for your phone)")
    tp = p["token"]
    if tp.exists():
        mode = tp.stat().st_mode & 0o777
        r.line("ok" if mode & 0o077 == 0 else "warn", f"access token file {tp} (mode {mode:o})"
               + ("" if mode & 0o077 == 0 else ": readable by other users, run chmod 600"))
    else:
        r.line("info", "no access token yet (created at first start)")
    if cfg["auth"]["loopback_exempt"]:
        r.line("warn", "auth.loopback_exempt = true: any program or account on this Mac can control it without the token")
    e = cfg["esp32"]
    if e["enabled"]:
        signed = bool(e.get("key"))
        r.line("info" if signed else "warn", f"ESP32 UDP listener enabled on {e['bind']}:{e['port']}"
               + (f" (bound {dbg.get('esp32_bound')})" if dbg and dbg.get("esp32_bound") else "")
               + (": signed datagrams only ([esp32] key)" if signed else
                  ": unauthenticated (no [esp32] key), anyone on that network can inject sensor data"))
    else:
        r.line("ok", "ESP32 UDP listener off")

    r.head("Data")
    d = p["data"]
    if d.exists():
        total = _du(d)
        free = shutil.disk_usage(d).free
        parts = ", ".join(f"{name} {_mb(_du(d / name) if (d / name).is_dir() else (d / name).stat().st_size)}"
                          for name in ("sentry.db", "raw", "walktests", "logs") if (d / name).exists())
        r.line("ok" if free > 2e9 else "warn", f"{d}: {_mb(total)}{f' ({parts})' if parts else ''}; {_mb(free)} free on disk")
    else:
        r.line("info", f"{d} does not exist yet")
    if p["log"].exists():
        errs = [ln for ln in p["log"].read_text(errors="replace").splitlines()[-2000:] if " ERROR " in ln]
        if errs:
            r.line("warn", f"{len(errs)} ERROR line(s) in the recent log; last: {errs[-1][:160]}")

    r.head("Start at login")
    if PLIST.exists():
        text = PLIST.read_text(errors="replace")
        mine = agent_is_mine()
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
        r.line("ok" if mine else "warn", f"LaunchAgent installed ({'loaded' if loaded else 'not loaded'})"
               + ("" if mine else f" but it points at another checkout: ./wifi-sentry autostart on to repoint it"))
    else:
        r.line("info", "off (./wifi-sentry autostart on)")

    print(f"\n{r.fails} problem(s), {r.warns} warning(s).")
    return 1 if r.fails else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sentry.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config file (default: config.toml + config.local.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("info", "pid", "token", "url", "open-url", "status", "port-check", "doctor", "agent-mine"):
        sub.add_parser(name)
    la = sub.add_parser("launchagent")
    la.add_argument("--uv-dir", default="")
    pr = sub.add_parser("probe")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--seconds", type=float, default=2.0)
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except ValueError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    fn = {"info": cmd_info, "pid": cmd_pid, "token": cmd_token, "url": cmd_url, "open-url": cmd_open_url, "status": cmd_status,
          "probe": cmd_probe, "port-check": cmd_port_check, "doctor": cmd_doctor,
          "launchagent": cmd_launchagent, "agent-mine": cmd_agent_mine}[a.cmd]
    return fn(cfg, a)


if __name__ == "__main__":
    try:
        rc = main()
        sys.stdout.flush()
    except BrokenPipeError:           # e.g. `./wifi-sentry url | head -1`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        rc = 0
    sys.exit(rc)
