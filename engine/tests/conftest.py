import gzip
import json
from pathlib import Path

import pytest

from sentry.config import PROJECT_ROOT, deep_merge, load_config
from sentry.replay import classify, run_replay
from sentry.sim import shift_rows

# 10 min of real idle wifisense output from the tested Mac (2026-11-23, room still), gzipped. The same
# capture is the --fake-mac recording and the example in docs/how-it-works.md.
IDLE = Path(__file__).parent / "fixtures" / "mac-idle-20261123-001939.jsonl.gz"
SPAN = 600.1  # seconds per pass of the idle file (600.0 s + one sample period)


@pytest.fixture(scope="session")
def idle_rows() -> list[dict]:
    with gzip.open(IDLE, "rt") as f:
        rows = [json.loads(line) for line in f]
    rows.sort(key=lambda r: r["t"])
    return rows


def looped(rows: list[dict], n: int) -> list[dict]:
    out = []
    for i in range(n):
        out += shift_rows(rows, i * SPAN)
    return out


def cfg_with(**sections) -> dict:
    """cfg_with(baseline={"learning_s": 180}, detect={"events": {...}})"""
    # Never let a test reach a real alert sink, whatever is in config(.local).toml.
    # Alert stubs listen on 127.0.0.1, which real installs refuse as a target.
    over: dict = {"alerts": {"ntfy": {"enabled": False}, "webhook": {"enabled": False},
                             "email": {"enabled": False}, "allow_loopback_targets": True}}
    for k, v in sections.items():
        over[k] = deep_merge(over.get(k, {}), v) if isinstance(v, dict) else v
    # The committed config.toml alone: never this machine's config.local.toml (ports, exposure, secrets).
    return load_config(PROJECT_ROOT / "config.toml", over)


def replay(rows: list[dict], cfg: dict, level: str = "medium", **kw) -> dict:
    return run_replay(classify(rows), cfg, sensitivity=level, **kw)


def events_after(res: dict, t: float) -> list[dict]:
    return [e for e in res["events"] if e["start"] >= t]


def tmp_path_str(p: Path) -> str:
    return str(p)


@pytest.fixture(autouse=True)
def _local_alert_stubs(monkeypatch):
    """Tests deliver alerts to stub servers on 127.0.0.1, which the engine refuses in real use."""
    import sentry.sinks
    monkeypatch.setattr(sentry.sinks, "ALLOW_LOOPBACK_TARGETS", True)
    monkeypatch.setattr(sentry.sinks, "SELF_BOUND", set())
