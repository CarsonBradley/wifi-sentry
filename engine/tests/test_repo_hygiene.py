"""The committed tree must carry nothing tied to one person's machine or network.

Generic patterns, not a list of anyone's real values (a list of real values would itself be the leak):
private and Tailscale addresses, home-directory paths, Tailscale MagicDNS and Bonjour host names, MAC
addresses, possessive computer names ("janes-macbook"), e-mail addresses, interpreter paths in
shebangs, and token-looking strings in documentation and config files.

Documentation and tests may use the placeholders in the allowlists below (RFC 5737 192.0.2.0/24 etc.
are never flagged). If you need another placeholder, add it here rather than weakening a pattern.

Checks every tracked file plus untracked files that aren't git-ignored (what `git add -A` would
commit). Gzipped fixtures are decompressed and checked too; images and lock files are skipped, and so
is this file (its pattern tests below are made-up examples of what must be caught).
"""
from __future__ import annotations

import gzip
import ipaddress
import re
import shutil
import subprocess

import pytest

from sentry.config import PROJECT_ROOT

# ---------------------------------------------------------------- allowlists (placeholders only)
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
ALLOWED_NETS = (
    ipaddress.ip_network("100.64.0.0/16"),     # placeholder Tailscale addresses in tests and docs
    ipaddress.ip_network("192.168.1.0/24"),    # placeholder LAN addresses in tests and docs
)
ALLOWED_HOME_NAMES = {"you", "me", "yourname", "name", "user", "USER", "Shared"}
ALLOWED_HOSTS = {"homeassistant.local", "wifi-sentry.local", "my-laptop.local", "tailnet-name.ts.net",
                 "your-mac.tailnet-name.ts.net"}
ALLOWED_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "aa:bb:cc:dd:ee:ff", "02:00:00:00:00:00"}
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", ".example", ".test", ".invalid",
                         "users.noreply.github.com", "anthropic.com")
ALLOWED_SHEBANGS = ("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env ")
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".apng", ".webp", ".ico", ".lock", ".bin"}
DOC_SUFFIXES = {".md", ".toml", ".yml", ".yaml", ".json", ".html", ".txt", ".svg"}

# ---------------------------------------------------------------- patterns
IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d])")
HOME = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")
HOSTNAME = re.compile(r"\b((?:[A-Za-z0-9-]+\.)+(?:ts\.net|local))(?!\.?[A-Za-z0-9-])")
MAC = re.compile(r"(?<![0-9A-Fa-f:])((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f:])")
COMPUTER_NAME = re.compile(r"\b(?!this-)[a-z]+s-(?:macbook|mac|imac|iphone|ipad)\b", re.I)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
# A url-safe token (mixed case and digits) or a long letters-and-digits run (hex, base32 topics).
TOKEN_MIXED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])")
TOKEN_ALNUM = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{20,}(?![A-Za-z0-9])")
PINNED_ACTION = re.compile(r"uses:\s*\S+@[0-9a-f]{40}\b")


def _is_mixed_token(s: str) -> bool:
    return any(c.isupper() for c in s) and any(c.islower() for c in s) and sum(c.isdigit() for c in s) >= 2


def _is_alnum_token(s: str) -> bool:
    return any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


def problems_in_line(line: str, doc: bool) -> list[str]:
    out = []
    for m in IPV4.finditer(line):
        try:
            ip = ipaddress.ip_address(m.group(0))
        except ValueError:
            continue
        if any(ip in n for n in ALLOWED_NETS):
            continue
        if ip in TAILSCALE_NET:
            out.append(f"Tailscale address {ip}")
        elif any(ip in n for n in RFC1918):
            out.append(f"private address {ip}")
    for m in HOME.finditer(line):
        if m.group(1) not in ALLOWED_HOME_NAMES and not set(m.group(1)) <= {"."}:
            out.append(f"home directory path {m.group(0)}")
    for m in HOSTNAME.finditer(line):
        if m.group(1).lower() not in ALLOWED_HOSTS:
            out.append(f"host name {m.group(1)}")
    for m in MAC.finditer(line):
        if m.group(1).lower().replace("-", ":") not in ALLOWED_MACS:
            out.append(f"MAC address {m.group(1)}")
    for m in COMPUTER_NAME.finditer(line):
        out.append(f"computer name {m.group(0)}")
    for m in EMAIL.finditer(line):
        if not m.group(1).lower().endswith(ALLOWED_EMAIL_DOMAINS):
            out.append(f"e-mail address {m.group(0)}")
    if doc:
        clean = PINNED_ACTION.sub("", line)
        for m in TOKEN_MIXED.finditer(clean):
            if _is_mixed_token(m.group(0)):
                out.append(f"token-like string {m.group(0)[:6]}…")
        for m in TOKEN_ALNUM.finditer(clean):
            if _is_alnum_token(m.group(0)):
                out.append(f"token-like string {m.group(0)[:6]}…")
    return out


def problems_in_text(name: str, text: str, doc: bool) -> list[str]:
    hits = []
    first = text.split("\n", 1)[0]
    if first.startswith("#!") and not first.startswith(ALLOWED_SHEBANGS):
        hits.append(f"{name}:1: machine-specific interpreter {first}")
    for i, line in enumerate(text.splitlines(), 1):
        hits += [f"{name}:{i}: {p}" for p in problems_in_line(line, doc)]
    return hits


SELF = "engine/tests/test_repo_hygiene.py"


def candidate_files() -> list[str]:
    git = ["git", "-C", str(PROJECT_ROOT)]
    tracked = subprocess.run(git + ["ls-files", "-z"], capture_output=True, text=True, check=True).stdout
    untracked = subprocess.run(git + ["ls-files", "-z", "--others", "--exclude-standard"],
                               capture_output=True, text=True, check=True).stdout
    return sorted({f for f in (tracked + untracked).split("\0") if f})


def test_committed_files_are_not_tied_to_one_machine():
    if shutil.which("git") is None or not (PROJECT_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    hits = []
    for f in candidate_files():
        p = PROJECT_ROOT / f
        if not p.is_file() or p.suffix.lower() in SKIP_SUFFIXES or f == SELF:
            continue
        if p.suffix == ".gz":
            text = gzip.decompress(p.read_bytes()).decode("utf-8", errors="replace")
        else:
            text = p.read_text(errors="replace")
        hits += problems_in_text(f, text, doc=p.suffix.lower() in DOC_SUFFIXES or p.name.startswith("config."))
    assert hits == [], "machine-specific values in the tree:\n" + "\n".join(hits[:50])


# ---------------------------------------------------------------- the patterns themselves
@pytest.mark.parametrize("line", [
    "hosts = [\"100.101.7.9\"]", "inet 10.0.4.2", "gateway 172.20.1.1", "lan 192.168.50.10",
    "cd /Users/alice/src/wifi-sentry", "open http://studio.tail1234.ts.net:8450/", "ping kitchen-mac.local",
    "bssid 3c:22:fb:01:02:03", "alices-macbook-pro.lan", "mail alice@gmail.com",
])
def test_patterns_catch_personal_values(line):
    assert problems_in_line(line, doc=False)


@pytest.mark.parametrize("line", [
    "token Zk3QvX9pL2mN8rT4wY6aB1cD5eF7gH0j", "url = \"https://ntfy.sh/sentry-k3nq7pfz2xw4m6ab5cd7ef2gh4\"",
])
def test_patterns_catch_tokens_in_docs(line):
    assert problems_in_line(line, doc=True)


@pytest.mark.parametrize("line", [
    "REMOTE = {\"Host\": \"100.64.10.20:8450\"}", "remote=\"192.168.1.20\"", "address 192.0.2.7", "127.0.0.1:8450",
    "don't commit `/Users/...` paths", "http://homeassistant.local:8123/api/webhook/...", "aa:bb:cc:dd:ee:ff",
    "the Mac's Wi-Fi", "curl http://<this-mac>:8450/api/status", "me@example.com", "c@d.example",
    "brew install python@3.12", "noreply@anthropic.com", "macOS 27.0.1 on Mac17,16",
    "uses: astral-sh/setup-uv@1db35dd8eee11c5894872e0b383ae6c406cf738d # v10.4.0",
    "[how-it-works.md §5](how-it-works.md#5-the-first-night-a-radio-artifact-that-looked-like-motion)",
    "IOReportCopyChannelsInGroup", "copy config.local.example.toml to config.local.toml", "netmask 255.255.255.0",
    "engine/tests/fixtures/mac-idle-20261123-001939.jsonl.gz",
])
def test_patterns_allow_placeholders_and_prose(line):
    assert problems_in_line(line, doc=True) == []
