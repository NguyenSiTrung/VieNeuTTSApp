"""Settings persistence: JSON round-trip, defaults, graceful corruption handling."""

import json
import logging
from pathlib import Path

import pytest

from vienetts_app.core.models import Settings
from vienetts_app.core.settings import load_settings, save_settings


class TestRoundTrip:
    def test_save_then_load_returns_equal_settings(self, tmp_path: Path) -> None:
        original = Settings(
            backend="onnx",
            precision="fp32",
            default_voice="Minh Đức",
            output_dir="/tmp/out",
            theme="dark",
            denoise_ref=False,
            temperature=0.8,
            model_repo="someone/vieneu-tts-custom",
        )
        path = save_settings(original, data_dir=tmp_path)
        assert path.is_file()
        assert load_settings(data_dir=tmp_path) == original

    def test_saved_file_is_json_with_all_fields(self, tmp_path: Path) -> None:
        path = save_settings(Settings(), data_dir=tmp_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data) == {
            "backend",
            "precision",
            "default_voice",
            "output_dir",
            "theme",
            "language",
            "denoise_ref",
            "temperature",
            "model_repo",
        }
        assert data["backend"] == "auto"
        assert data["model_repo"] == ""

    def test_old_settings_file_without_model_repo_loads_default(self, tmp_path: Path) -> None:
        # Pre-model_repo settings.json (written by an older app version).
        legacy = {
            "backend": "onnx",
            "precision": "int8",
            "default_voice": "Adam",
            "output_dir": "",
            "theme": "system",
            "language": "system",
            "denoise_ref": True,
            "temperature": 0.4,
        }
        (tmp_path / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.model_repo == ""
        assert loaded.backend == "onnx"


class TestDefaults:
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()

    def test_missing_directory_returns_defaults(self, tmp_path: Path) -> None:
        assert load_settings(data_dir=tmp_path / "nonexistent" / "deeper") == Settings()

    def test_save_creates_missing_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b"
        path = save_settings(Settings(theme="light"), data_dir=target)
        assert path.is_file()
        assert load_settings(data_dir=target).theme == "light"


class TestAtomicity:
    def test_failed_save_keeps_previous_file_intact(self, tmp_path: Path, monkeypatch) -> None:
        # Regression: save wrote in place, so a crash mid-write truncated the
        # live file and the next load silently wiped every setting. The write
        # is now temp + os.replace: the old file survives a failed replace.
        import vienetts_app.core.settings as settings_module

        original = Settings(theme="dark", temperature=0.9)
        save_settings(original, data_dir=tmp_path)

        def boom(_src, _dst):
            raise OSError("disk vanished")

        monkeypatch.setattr(settings_module.os, "replace", boom)
        with pytest.raises(OSError, match="disk vanished"):
            save_settings(Settings(theme="light"), data_dir=tmp_path)

        assert load_settings(data_dir=tmp_path) == original  # untouched
        assert not (tmp_path / "settings.json.tmp").exists()  # temp cleaned up

    def test_successful_save_leaves_no_temp_file(self, tmp_path: Path) -> None:
        save_settings(Settings(), data_dir=tmp_path)
        assert list(tmp_path.glob("*.tmp")) == []


class TestCorruptOrInvalid:
    def test_unusable_file_returns_defaults_with_warning(self, tmp_path: Path, caplog) -> None:
        # Any unusable settings file — corrupt JSON, out-of-range values, or
        # unknown fields — degrades to defaults instead of raising, with a
        # warning; a non-dict payload degrades silently.
        (tmp_path / "settings.json").write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()
        assert any("settings" in r.message.lower() for r in caplog.records)

        (tmp_path / "settings.json").write_text(
            json.dumps({"backend": "cuda", "precision": "int4", "theme": "solarized"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()
        assert caplog.records

        (tmp_path / "settings.json").write_text(
            json.dumps({"backend": "onnx", "future_field": 1}), encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()
        assert caplog.records

        (tmp_path / "settings.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert load_settings(data_dir=tmp_path) == Settings()


class TestDefaultLocation:
    def test_uses_platformdirs_when_no_dir_given(self, tmp_path: Path, monkeypatch) -> None:
        import platformdirs

        monkeypatch.setattr(platformdirs, "user_data_dir", lambda app: str(tmp_path / "userdata"))
        save_settings(Settings(default_voice="Adam"), data_dir=None)
        assert (tmp_path / "userdata" / "settings.json").is_file()
        assert load_settings(data_dir=None).default_voice == "Adam"

    def test_partial_valid_fields_load(self, tmp_path: Path) -> None:
        (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.theme == "dark"
        assert loaded.backend == "auto"  # unspecified fields keep defaults


def test_theme_and_language_round_trips(tmp_path: Path) -> None:
    for theme in ("system", "light", "dark"):
        save_settings(Settings(theme=theme), data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path).theme == theme
    for language in ("system", "vi", "en"):
        save_settings(Settings(language=language), data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path).language == language
    assert Settings().language == "system"


def test_invalid_language_returns_defaults_with_warning(tmp_path: Path, caplog) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"language": "fr"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        loaded = load_settings(data_dir=tmp_path)
    assert loaded == Settings()
    assert caplog.records


def test_partial_fields_keep_language_default(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    loaded = load_settings(data_dir=tmp_path)
    assert loaded.theme == "dark"
    assert loaded.language == "system"
