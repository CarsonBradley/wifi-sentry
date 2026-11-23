"""Turning bind specs ("loopback", "tailscale", "lan", literal addresses) into addresses.

No sudo, no scans, nothing changed on the system: the Tailscale address comes from the `tailscale`
CLI (any of the usual install locations, including the copy bundled inside the Mac app), the
Wi-Fi interface and its address from `sensor/wifisense --iface` (CoreWLAN, so the interface is
found whatever its BSD name), with read-only `networksetup` / `ifconfig` as fallbacks.
Every function returns None when the address isn't available (yet): the engine retries."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("sentry.net")

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",   # Mac app (App Store / standalone): bundled CLI
    "/opt/homebrew/bin/tailscale",                            # Homebrew, Apple silicon
    "/usr/local/bin/tailscale",                               # Homebrew Intel, or the app's "install CLI"
    "~/.local/bin/tailscale",
    "~/go/bin/tailscale",                                     # open-source tailscaled built from source
)


def _run(cmd: list[str], timeout: float = 4.0) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def find_tailscale() -> str | None:
    cands = [shutil.which("tailscale")] + [os.path.expanduser(p) for p in TAILSCALE_PATHS]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def tailscale_ipv4() -> str | None:
    """This Mac's Tailscale IPv4 (100.64.0.0/10), or None if Tailscale isn't installed / up."""
    cli = find_tailscale()
    if cli:
        out = _run([cli, "ip", "-4"])
        for line in (out or "").split():
            if _in_tailnet(line):
                return line
        return None
    # No CLI anywhere: accept a single 100.64/10 address on a utun interface (Tailscale's tunnel).
    found = [a for name, a in ifconfig_ipv4() if name.startswith("utun") and _in_tailnet(a)]
    return found[0] if len(found) == 1 else None


def _in_tailnet(s: str) -> bool:
    try:
        return ipaddress.ip_address(s) in TAILSCALE_NET
    except ValueError:
        return False


def ifconfig_ipv4() -> list[tuple[str, str]]:
    out = _run(["/sbin/ifconfig"]) or ""
    res, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9]+):", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if m and cur:
            res.append((cur, m.group(1)))
    return res


def wifi_info(root: str | Path | None = None) -> dict:
    """{"iface", "ipv4", "link"} of the Wi-Fi interface (values None when unknown)."""
    if root is not None:
        exe = Path(root) / "sensor" / "wifisense"
        if exe.is_file() and os.access(exe, os.X_OK):
            out = _run([str(exe), "--iface"])
            if out:
                try:
                    d = json.loads(out.strip().splitlines()[-1])
                    if isinstance(d, dict):
                        return {"iface": d.get("iface"), "ipv4": d.get("ipv4"), "link": d.get("link")}
                except (ValueError, IndexError):
                    pass
    # Fallback without the sampler: the hardware port called "Wi-Fi" (older macOS: "AirPort").
    iface = None
    out = _run(["/usr/sbin/networksetup", "-listallhardwareports"]) or ""
    port = None
    for line in out.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port in ("Wi-Fi", "AirPort"):
            iface = line.split(":", 1)[1].strip()
            break
    ipv4 = None
    if iface:
        ipv4 = (_run(["/usr/sbin/ipconfig", "getifaddr", iface]) or "").strip() or None
    return {"iface": iface, "ipv4": ipv4, "link": None}


def lan_ipv4(root: str | Path | None = None) -> str | None:
    return wifi_info(root).get("ipv4")


def resolve_spec(spec: str, root: str | Path | None = None) -> str | None:
    """Address for a bind spec, or None if it isn't available right now."""
    if spec in ("loopback", "localhost"):
        return "127.0.0.1"
    if spec == "tailscale":
        return tailscale_ipv4()
    if spec == "lan":
        return lan_ipv4(root)
    return spec


def is_loopback_addr(a: str | None) -> bool:
    if not a:
        return False
    try:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback
