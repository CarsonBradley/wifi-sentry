"""wifi-sentry engine.

The version has one source: `version` in engine/pyproject.toml. It is read from there (the engine
always runs from its source tree), so a `git pull` shows the new version even before `uv sync`.
sensor/build.sh stamps the same string into the wifisense binary, and ./wifi-sentry reads it too.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


def _read_version() -> str:
    try:
        with open(Path(__file__).resolve().parents[2] / "pyproject.toml", "rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        try:
            from importlib.metadata import version
            return version("wifi-sentry-engine")
        except Exception:  # noqa: BLE001
            return "unknown"


__version__ = _read_version()
