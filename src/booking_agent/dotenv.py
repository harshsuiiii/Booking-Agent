"""Minimal .env loader (no python-dotenv dependency). Real env vars always win."""

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
