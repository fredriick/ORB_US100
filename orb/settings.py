from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def config_path(default: str | None = None) -> str:
    """Config file path: `--config` arg > ORB_CONFIG env > ./config.json."""
    return default or os.environ.get("ORB_CONFIG", str(BASE / "config.json"))


def data_dir() -> Path:
    """Directory for runtime artifacts (trades.csv, bot.log). ORB_DATA_DIR env."""
    path = Path(os.environ.get("ORB_DATA_DIR", str(BASE)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact(name: str) -> Path:
    return data_dir() / name
