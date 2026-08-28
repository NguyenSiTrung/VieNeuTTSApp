"""Settings persistence: JSON round-trip, defaults, graceful corruption handling."""

import json
import logging
from pathlib import Path

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
        }
        assert data["backend"] == "auto"


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


class TestCorruptOrInvalid:
    def test_corrupt_json_returns_defaults_with_warning(self, tmp_path: Path, caplog) -> None:
        (tmp_path / "settings.json").write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()
        assert any("settings" in r.message.lower() for r in caplog.records)

    def test_invalid_values_return_defaults_with_warning(self, tmp_path: Path, caplog) -> None:
        (tmp_path / "settings.json").write_text(
            json.dumps({"backend": "cuda", "precision": "int4", "theme": "solarized"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()
        assert caplog.records

    def test_unknown_fields_return_defaults_with_warning(self, tmp_path: Path, caplog) -> None:
        (tmp_path / "settings.json").write_text(
            json.dumps({"backend": "onnx", "future_field": 1}), encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()
        assert caplog.records

    def test_non_dict_json_returns_defaults(self, tmp_path: Path) -> None:
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
    (tmp_path / "settings.json").write_text(
        json.dumps({"language": "fr"}), encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        loaded = load_settings(data_dir=tmp_path)
    assert loaded == Settings()
    assert caplog.records


def test_partial_fields_keep_language_default(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    loaded = load_settings(data_dir=tmp_path)
    assert loaded.theme == "dark"
    assert loaded.language == "system"
