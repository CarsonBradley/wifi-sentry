"""Configuration: built-in defaults deep-merged with config.toml (committed, generic) and
config.local.toml (git-ignored, this machine's settings and secrets)."""
from __future__ import annotations

import copy
import ipaddress
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Words accepted in server.expose / server.hosts / esp32.bind besides literal IP addresses.
BIND_KEYWORDS = ("loopback", "tailscale", "lan")
WILDCARDS = ("0.0.0.0", "::", "*", "")

DEFAULTS: dict = {
    # 127.0.0.1 is always bound. `expose` adds more: "tailscale", "lan" or literal addresses.
    "server": {"port": 8450, "expose": [], "web_dir": "web"},
    "auth": {
        # True lets requests from this Mac itself (loopback peer, loopback Host header, no proxy headers)
        # skip the token. Off: other programs and accounts on the Mac can reach 127.0.0.1 too.
        "loopback_exempt": False,
        "token_file": "access-token",     # relative to paths.data_dir
        "max_failures": 10,               # wrong tokens per client address within failure_window_s
        "failure_window_s": 300,
        "cookie_days": 30,
    },
    "paths": {"data_dir": "data"},
    "mac": {
        "enabled": True,
        "command": ["sensor/wifisense", "--hz", "10"],
        "restart_min_s": 1.0,
        "restart_max_s": 60.0,
        "weight": 1.0,
    },
    # key: shared secret the boards sign with (esp32/README.md); "" accepts unsigned datagrams.
    "esp32": {"enabled": False, "bind": "lan", "port": 5566, "weight": 1.0, "key": ""},
    "sources": {"offline_after_s": 5.0, "gap_reset_s": 1.5, "channel_settle_s": 5.0, "mac_frozen_s": 15.0,
                "mac_stuck_s": 600.0},
    "detect": {
        "mac_window_s": 3.0,
        "esp32_window_s": 2.0,
        "mac_common_weight": 1.0,
        "mac_step_robust": True,
        "mac_pulse_reject": True,
        "mac_pulse_max_s": 2.0,
        "mac_pulse_min_db": 2.0,
        "mac_pulse_max_diff_db": 1.0,
        "mad_k": 3.0,
        "mac_abs_floor": 0.15,
        "mac_rel_floor": 0.0,
        "esp32_abs_floor": 0.002,
        "esp32_rel_floor": 0.25,
        "thresholds": {"low": 5.0, "medium": 3.0, "high": 2.0},
        "events": {
            "sensitivity": "medium",
            "off_ratio": 0.6,
            "min_duration_s": 2.0,
            "end_timeout_s": 10.0,
            "merge_gap_s": 30.0,
        },
        "aux": {
            "enabled": True,
            "z0": 3.0,
            "k": 0.25,
            "min_factor": 0.5,
            "w_noise": 1.0,
            "w_cca": 0.7,
            "w_cca_intf": 1.0,
            "w_retrans": 0.0,
        },
    },
    "baseline": {
        "learning_s": 600,
        "min_learning_ticks": 30,
        "window_s": 3600,
        "recompute_every_s": 10,
        "holdoff_s": 60,
        "gate_ratio": 0.5,
        "absorb_after_s": 900,
        "absorb_window_s": 300,
        "absorb_min_frac": 0.9,
        "absorb_max_cv": 0.5,
        "force_absorb_after_s": 0,
        "save_every_s": 60,
        "max_age_s": 86400,
    },
    # Exit delay for a manual arm from the dashboard (the arm switch); the dashboard can change it,
    # and the weekly schedule, at run time (stored in the database).
    "arming": {"delay_s": 60},
    "alerts": {
        "cooldown_s": 300,
        "notify_on_close": False,
        # Armed but not watching (sensor offline / blind / learning) this long: one "can't sense" alert, and
        # "watching again" when it recovers; also "was not running" after a down time this long. 0 = off.
        "not_watching_s": 120,
        "not_watching_cooldown_s": 1800,
        # Alert targets on this Mac's loopback (a self-hosted ntfy in Docker, test stubs). Off: a webhook
        # pointed at 127.0.0.1 could reach other local services. Config file only, never via the API.
        "allow_loopback_targets": False,
        "base_url": "",
        "ntfy": {"enabled": False, "url": "", "priority": "high", "token": ""},
        "webhook": {"enabled": False, "url": "", "format": "json"},
        "email": {
            "enabled": False,
            "host": "",
            "port": 587,
            "starttls": True,
            "username": "",
            "password": "",
            "from": "",
            "to": [],
        },
    },
    "storage": {"history_keep_days": 7},
    "record": {"enabled": True, "include_esp32": False, "keep_hours": 24, "max_bytes": 2_000_000_000},
    "walktest": {"max_duration_s": 180, "reliable_fraction": 0.2},
}

LEVELS = ("low", "medium", "high")


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    """Load config.toml (default: project root, plus config.local.toml if present) over DEFAULTS,
    then apply overrides."""
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else PROJECT_ROOT / "config.toml"
    files = [p]
    if not path:
        # Optional, git-ignored overlay for secrets (ntfy topic, webhook URL, SMTP password) and
        # local overrides, so config.toml itself can stay committed.
        files.append(PROJECT_ROOT / "config.local.toml")
    loaded = []
    for f_path in files:
        if f_path.exists():
            with open(f_path, "rb") as f:
                try:
                    cfg = deep_merge(cfg, tomllib.load(f))
                except tomllib.TOMLDecodeError as e:
                    raise ValueError(f"{f_path.name}: {e}") from None
            loaded.append(str(f_path))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    cfg["_root"] = str(PROJECT_ROOT)
    cfg["_files"] = loaded
    cfg["_notes"] = migrate(cfg)
    validate(cfg)
    return cfg


def migrate(cfg: dict) -> list[str]:
    """Translate settings from older config files in place; return notes for the log / doctor.

    0.1.x: [server] hosts = ["127.0.0.1", "100.x.y.z"] was the full bind list (the Tailscale address
    hard-coded). It still works as an explicit list; the new way is expose = ["tailscale"]."""
    notes = []
    srv = cfg["server"]
    if srv.get("hosts") is not None:
        hosts = [str(h) for h in srv["hosts"]]
        srv["hosts"] = hosts
        ts = [h for h in hosts if _is_tailscale_ip(h)]
        notes.append("[server] hosts is deprecated (used as the complete bind list); use expose = "
                     + ('["tailscale"]' if ts else "[...]") + " instead")
    esp = cfg["esp32"]
    if esp.get("enabled") and str(esp.get("bind")) in WILDCARDS:
        notes.append("[esp32] bind = \"0.0.0.0\": the ESP32 UDP port listens on every interface"
                     + ("" if cfg.get("esp32", {}).get("key") else " and is unauthenticated (no [esp32] key)"))
    return notes


def _is_tailscale_ip(h: str) -> bool:
    try:
        return ipaddress.ip_address(h) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def bind_specs(cfg: dict) -> list[str]:
    """What the HTTP server should listen on, as specs: literal addresses or BIND_KEYWORDS.
    Loopback always comes first, unless a legacy/explicit server.hosts list says otherwise."""
    srv = cfg["server"]
    if srv.get("bind_override"):           # --hosts on the command line
        specs = list(srv["bind_override"])
    elif srv.get("hosts") is not None:     # legacy 0.1.x full list
        specs = list(srv["hosts"])
    else:
        specs = ["loopback"] + [str(x) for x in srv.get("expose") or []]
    out = []
    for sp in specs:
        sp = "loopback" if sp in ("127.0.0.1", "localhost") else sp
        if sp not in out:
            out.append(sp)
    return out


def check_bind_spec(spec, where: str, allow_wildcard: bool = False) -> None:
    if not isinstance(spec, str):
        raise ValueError(f"{where}: {spec!r} must be a string")
    if spec in BIND_KEYWORDS or spec == "localhost":
        return
    if spec in WILDCARDS:
        if allow_wildcard and spec == "0.0.0.0":
            return
        raise ValueError(f"{where}: {spec!r} would listen on every network interface; wifi-sentry never does "
                         "that. Use \"lan\", \"tailscale\" or this Mac's address on that network.")
    try:
        ipaddress.ip_address(spec)
    except ValueError:
        raise ValueError(f"{where}: {spec!r} is not an IP address or one of {', '.join(BIND_KEYWORDS)}") from None


def validate(cfg: dict) -> None:
    th = cfg["detect"]["thresholds"]
    for lv in LEVELS:
        if float(th[lv]) <= 0:
            raise ValueError(f"detect.thresholds.{lv} must be > 0")
    if cfg["detect"]["events"]["sensitivity"] not in LEVELS:
        raise ValueError("detect.events.sensitivity must be low|medium|high")
    srv = cfg["server"]
    if not isinstance(srv.get("expose"), list):
        raise ValueError("server.expose must be a list, e.g. [] or [\"tailscale\"]")
    if srv.get("hosts") is not None and not isinstance(srv["hosts"], list):
        raise ValueError("server.hosts must be a list")
    for spec in srv.get("expose") or []:
        check_bind_spec(spec, "server.expose")
    for spec in srv.get("hosts") or []:
        check_bind_spec(spec, "server.hosts")
    for spec in srv.get("bind_override") or []:
        check_bind_spec(spec, "--hosts")
    for name, sec in (("server.port", srv), ("esp32.port", cfg["esp32"])):
        p = sec.get("port")
        if not isinstance(p, int) or isinstance(p, bool) or not 0 <= p <= 65535:
            raise ValueError(f"{name} must be an integer port number")
    # The ESP32 listener may be told "0.0.0.0" explicitly (documented as unauthenticated, LAN-wide).
    check_bind_spec(str(cfg["esp32"]["bind"]), "esp32.bind", allow_wildcard=True)
    if not isinstance(cfg["esp32"].get("key", ""), str):
        raise ValueError('esp32.key must be a string (the shared key, or "" for none)')
    for k in ("not_watching_s", "not_watching_cooldown_s"):
        v = cfg["alerts"].get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"alerts.{k} must be a number of seconds >= 0")


def resolve(cfg: dict, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else Path(cfg["_root"]) / p
