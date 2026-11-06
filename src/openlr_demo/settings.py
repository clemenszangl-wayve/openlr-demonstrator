"""Local settings: environment variables, falling back to a git-ignored ``.env`` file."""

from __future__ import annotations

import os
from pathlib import Path

from . import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / ".env"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    path = path or ENV_FILE
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def setting(name: str, default: str | None = None) -> str | None:
    """An environment variable (even if empty) wins over the ``.env`` file."""
    if name in os.environ:
        return os.environ[name]
    return read_env_file().get(name, default)
