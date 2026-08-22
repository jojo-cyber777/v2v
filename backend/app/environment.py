"""Project-wide environment loading performed before backend submodules import."""

from __future__ import annotations

import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
XMAX_PERMANENT_API_KEY_ENV = "XMAX_PERMANENT_API_KEY"
XMAX_TEMP_KEY_ENCRYPTION_KEY_ENV = "XMAX_TEMP_KEY_ENCRYPTION_KEY"


def load_project_environment(env_path: Path | None = None) -> None:
    """Load project .env values without replacing the process environment."""
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return

    file_values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENVIRONMENT_KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        file_values[key] = value

    for key, value in file_values.items():
        os.environ.setdefault(key, value)


def get_xmax_permanent_api_key() -> str:
    """Return the server-only Xmax credential without logging or serializing it."""
    return os.getenv(XMAX_PERMANENT_API_KEY_ENV, "").strip()


def get_xmax_temp_key_encryption_key() -> str:
    """Return the backend-only temporary-key encryption key."""
    return os.getenv(XMAX_TEMP_KEY_ENCRYPTION_KEY_ENV, "").strip()


def get_xmax_setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default
