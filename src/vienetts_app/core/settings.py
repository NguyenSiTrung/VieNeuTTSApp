"""Settings persistence: JSON in the platform data dir (§9).

The data dir is injectable so tests never touch the real user profile.
Corrupt, invalid, or unknown-content files degrade to defaults with a logged
warning — the app must never crash over a settings file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path

import platformdirs

from vienetts_app.core.models import Settings

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"
APP_NAME = "VieNeuTTSApp"


def _migrate_legacy_data_dir(target: Path) -> None:
    try:
        legacy = Path(platformdirs.user_data_dir(APP_NAME))
        if legacy == target or not legacy.is_dir():
            return
        target.mkdir(parents=True, exist_ok=True)
        legacy_settings = legacy / SETTINGS_FILENAME
        target_settings = target / SETTINGS_FILENAME
        if legacy_settings.is_file() and not target_settings.is_file():
            shutil.copy2(legacy_settings, target_settings)
        legacy_voices = legacy / "voices"
        target_voices = target / "voices"
        if legacy_voices.is_dir() and not target_voices.exists():
            shutil.copytree(legacy_voices, target_voices)
        legacy_models = legacy / "models" / "official-v1"
        target_models = target / "models" / "official-v1"
        if (legacy_models / "install.json").is_file() and not target_models.exists():
            target_models.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy_models, target_models)
    except Exception as exc:  # pragma: no cover
        logger.debug("Legacy data dir migration skipped: %s", exc)


def default_data_dir() -> Path:
    target = Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    _migrate_legacy_data_dir(target)
    return target


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
    """Write ``settings`` as JSON into ``data_dir`` (created if needed).

    Atomic (temp file + ``os.replace``, same pattern as the audiobook
    workspace): a crash mid-write can never truncate the live file and wipe
    every setting back to defaults.
    """
    path = _settings_path(default_data_dir() if data_dir is None else Path(data_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings), indent=2)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(payload, encoding="utf-8")
        for attempt in range(4):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.05)
    except OSError:
        # Best-effort cleanup of the orphaned temp file; the failure itself
        # propagates (callers surface it as errorText).
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise
    return path
