"""Settings persistence: JSON in the platform data dir (§9).

The data dir is injectable so tests never touch the real user profile.
Corrupt, invalid, or unknown-content files degrade to defaults with a logged
warning — the app must never crash over a settings file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import platformdirs

from vienetts_app.core.models import Settings

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"
APP_NAME = "VieNeuTTSApp"


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


def _settings_path(data_dir: Path) -> Path:
    return data_dir / SETTINGS_FILENAME


def load_settings(data_dir: Path | None = None) -> Settings:
    """Load settings from ``data_dir`` (default: platform data dir).

    Missing file or directory → defaults. Corrupt JSON, non-dict JSON, unknown
    fields, or values that fail validation → defaults + logged warning.
    """
    path = _settings_path(default_data_dir() if data_dir is None else Path(data_dir))
    if not path.is_file():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"expected a JSON object, got {type(data).__name__}")
        return Settings(**data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Ignoring invalid settings file %s (%s); using defaults", path, exc)
        return Settings()


def save_settings(settings: Settings, data_dir: Path | None = None) -> Path:
    """Write ``settings`` as JSON into ``data_dir`` (created if needed)."""
    path = _settings_path(default_data_dir() if data_dir is None else Path(data_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    return path
