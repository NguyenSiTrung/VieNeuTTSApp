"""Settings persistence: JSON round-trip, defaults, graceful corruption handling."""

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from vienetts_app.core.models import Settings
from vienetts_app.core.settings import load_settings, save_settings


class TestRoundTrip:
    def test_settings_round_trip(self, tmp_path: Path) -> None:
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

        path = save_settings(Settings(), data_dir=tmp_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data) == {
            "backend",
            "precision",
            "default_voice",
            "output_dir",
            "export_format",
            "theme",
            "language",
            "denoise_ref",
            "live_preview",
            "temperature",
            "speed",
            "silence_p",
            "model_repo",
            "model_cache_enabled",
            "engine_profile",
            "qwen_device",
            "qwen_model_format",
            "qwen_gguf_quantization",
            "qwen_gguf_device",
            "synthesis_language",
            "ort_intra_op_threads",
            "ort_step_single_thread",
            "ort_spin_during_job",
            "blas_threads",
            "window_x",
            "window_y",
            "window_width",
            "window_height",
            "window_maximized",
        }
        assert data["backend"] == "auto"
        assert data["model_repo"] == ""
        assert data["model_cache_enabled"] is True
        assert data["window_x"] is None
        assert data["window_maximized"] is False

        original = Settings(window_x=120, window_y=64, window_width=1280, window_height=800)
        save_settings(original, data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path) == original
        maximized = replace(original, window_maximized=True)
        save_settings(maximized, data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path).window_maximized is True

    def test_legacy_and_invalid_files(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Settings(window_x=1.5)
        with pytest.raises(ValueError):
            Settings(window_width=True)
        with pytest.raises(ValueError):
            Settings(window_maximized="yes")

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
    def test_defaults_and_directory_creation(self, tmp_path: Path) -> None:
        assert load_settings(data_dir=tmp_path) == Settings()
        assert load_settings(data_dir=tmp_path / "nonexistent" / "deeper") == Settings()

        target = tmp_path / "a" / "b"
        path = save_settings(Settings(theme="light"), data_dir=target)
        assert path.is_file()
        assert load_settings(data_dir=target).theme == "light"


class TestAtomicity:
    def test_atomic_save_contract(self, tmp_path: Path, monkeypatch) -> None:
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

        monkeypatch.undo()
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
    def test_platformdirs_location(self, tmp_path: Path, monkeypatch) -> None:
        import platformdirs

        monkeypatch.setattr(
            platformdirs, "user_data_dir", lambda app, *a, **kw: str(tmp_path / "userdata")
        )
        save_settings(Settings(default_voice="Adam"), data_dir=None)
        assert (tmp_path / "userdata" / "settings.json").is_file()
        assert load_settings(data_dir=None).default_voice == "Adam"

        import platformdirs

        from vienetts_app.core.settings import APP_NAME, default_data_dir

        recorded_calls = []

        def fake_user_data_dir(appname, appauthor=None, **kwargs):
            recorded_calls.append({"appname": appname, "appauthor": appauthor})
            return f"/tmp/fake-{appname}"

        monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)
        dir_path = default_data_dir()
        assert dir_path == Path(f"/tmp/fake-{APP_NAME}")
        assert recorded_calls[0] == {"appname": APP_NAME, "appauthor": False}

    def test_legacy_and_partial_files(self, tmp_path: Path, monkeypatch) -> None:
        import platformdirs

        from vienetts_app.core.settings import APP_NAME, default_data_dir

        new_dir = tmp_path / "AppData" / "Local" / APP_NAME
        legacy_dir = new_dir / APP_NAME
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        (legacy_dir / "voices").mkdir()
        (legacy_dir / "voices" / "voices.json").write_text('{"v": 1}', encoding="utf-8")
        (legacy_dir / "models" / "official-v1").mkdir(parents=True)
        (legacy_dir / "models" / "official-v1" / "install.json").write_text("{}", encoding="utf-8")

        def fake_user_data_dir(appname, appauthor=None, **kwargs):
            if appauthor is False:
                return str(new_dir)
            return str(legacy_dir)

        monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)
        resolved = default_data_dir()
        assert resolved == new_dir
        assert (new_dir / "settings.json").read_text(encoding="utf-8") == json.dumps(
            {"theme": "dark"}
        )
        assert (new_dir / "voices" / "voices.json").read_text(encoding="utf-8") == '{"v": 1}'
        assert (new_dir / "models" / "official-v1" / "install.json").read_text(
            encoding="utf-8"
        ) == "{}"

        (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.theme == "dark"
        assert loaded.backend == "auto"  # unspecified fields keep defaults
        assert loaded.language == "system"


def test_settings_field_round_trips(tmp_path: Path) -> None:
    for theme in ("system", "light", "dark"):
        save_settings(Settings(theme=theme), data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path).theme == theme
    for language in ("system", "vi", "en"):
        save_settings(Settings(language=language), data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path).language == language
    assert Settings().language == "system"
    save_settings(Settings(model_cache_enabled=False), data_dir=tmp_path)
    assert load_settings(data_dir=tmp_path).model_cache_enabled is False
    save_settings(Settings(), data_dir=tmp_path)
    assert load_settings(data_dir=tmp_path).model_cache_enabled is True
    assert Settings().live_preview is False
    save_settings(Settings(live_preview=True), data_dir=tmp_path)
    assert load_settings(data_dir=tmp_path).live_preview is True
    save_settings(Settings(live_preview=False), data_dir=tmp_path)
    assert load_settings(data_dir=tmp_path).live_preview is False
    with pytest.raises(ValueError):
        Settings(live_preview="yes")


def test_invalid_language_returns_defaults_with_warning(tmp_path: Path, caplog) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"language": "fr"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        loaded = load_settings(data_dir=tmp_path)
    assert loaded == Settings()
    assert caplog.records

    with caplog.at_level(logging.WARNING):
        loaded = load_settings(data_dir=tmp_path)
    assert loaded == Settings()
    assert caplog.records

    with caplog.at_level(logging.WARNING):
        loaded = load_settings(data_dir=tmp_path)
    assert loaded == Settings()
    assert caplog.records

    with caplog.at_level(logging.WARNING):
        loaded = load_settings(data_dir=tmp_path)
    assert loaded == Settings()
    assert caplog.records

    with caplog.at_level(logging.WARNING):
        loaded = load_settings(data_dir=tmp_path)
    assert loaded == Settings()
    assert caplog.records


class TestEngineProfileMigration:
    """The global engine profile must migrate without losing any other field."""

    def test_engine_field_migration(self, tmp_path: Path) -> None:
        legacy = {
            "backend": "onnx",
            "precision": "fp32",
            "default_voice": "Minh Đức",
            "output_dir": "/tmp/out",
            "export_format": "mp3",
            "theme": "dark",
            "language": "vi",
            "denoise_ref": False,
            "temperature": 0.9,
            "speed": 1.25,
            "silence_p": 0.3,
            "live_preview": True,
            "model_repo": "owner/vieneu-custom",
            "model_cache_enabled": False,
            "window_x": 10,
            "window_y": 20,
            "window_width": 1280,
            "window_height": 800,
            "window_maximized": True,
        }
        (tmp_path / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")

        loaded = load_settings(data_dir=tmp_path)

        assert loaded.engine_profile == "vieneu"
        assert loaded.qwen_device == "auto"
        for field, value in legacy.items():
            assert getattr(loaded, field) == value, field

        from vienetts_app.core.engine_profiles import list_profiles

        for profile in list_profiles():
            save_settings(Settings(engine_profile=profile), data_dir=tmp_path)
            assert load_settings(data_dir=tmp_path).engine_profile == profile
        for device in ("auto", "cpu", "cuda", "mps"):
            save_settings(Settings(qwen_device=device), data_dir=tmp_path)
            assert load_settings(data_dir=tmp_path).qwen_device == device

    def test_engine_field_clamping(self, tmp_path: Path, caplog) -> None:
        # An older/renamed profile id must not wipe the rest of the file.
        (tmp_path / "settings.json").write_text(
            json.dumps({"engine_profile": "qwen_customvoice", "theme": "dark"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded.engine_profile == "vieneu"
        assert loaded.theme == "dark"
        assert any("engine_profile" in r.message for r in caplog.records)

        (tmp_path / "settings.json").write_text(
            json.dumps({"qwen_device": "tpu", "output_dir": "/tmp/keep"}), encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded.qwen_device == "auto"
        assert loaded.output_dir == "/tmp/keep"
        assert any("qwen_device" in r.message for r in caplog.records)

        # The language is profile-scoped: a code the active profile cannot
        # serve is dropped (the profile default applies) while everything else
        # in the file survives.
        (tmp_path / "settings.json").write_text(
            json.dumps({"synthesis_language": "vi", "theme": "dark"}), encoding="utf-8"
        )
        assert load_settings(data_dir=tmp_path).synthesis_language == "vi"

        (tmp_path / "settings.json").write_text(
            json.dumps({"engine_profile": "qwen_custom_0_6b", "synthesis_language": "vi"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded.engine_profile == "qwen_custom_0_6b"
        assert loaded.synthesis_language == ""
        assert any("synthesis_language" in r.message for r in caplog.records)

        # An unknown profile id falls back to VieNeu, and the language is
        # clamped against THAT profile, not against the stale id.
        (tmp_path / "settings.json").write_text(
            json.dumps({"engine_profile": "qwen_9b", "synthesis_language": "vi"}),
            encoding="utf-8",
        )
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.engine_profile == "vieneu"
        assert loaded.synthesis_language == "vi"


class TestQwenVariantMigration:
    """Model-format fields migrate to the GGUF default and clamp independently."""

    def test_variant_field_migration(self, tmp_path: Path) -> None:
        # A file written before the variant fields existed keeps every
        # recorded preference and defaults to GGUF — the Qwen family's
        # recommended format (native pack + 0.6–1.0 GB talker instead of a
        # multi-GB PyTorch runtime plus ~2.5 GB of full weights).
        (tmp_path / "settings.json").write_text(
            json.dumps(
                {
                    "engine_profile": "qwen_base_0_6b",
                    "qwen_device": "cuda",
                    "default_voice": "Minh Đức",
                    "theme": "dark",
                }
            ),
            encoding="utf-8",
        )
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.engine_profile == "qwen_base_0_6b"
        assert loaded.qwen_device == "cuda"
        assert loaded.qwen_model_format == "gguf"
        assert loaded.qwen_gguf_quantization == "Q8_0"
        assert loaded.qwen_gguf_device == "auto"
        assert loaded.default_voice == "Minh Đức"
        assert loaded.theme == "dark"

        # The default applies where nothing is stored: a user who picked the
        # official weights keeps them across loads (the format is a persisted
        # choice, not a value the default re-imposes).
        (tmp_path / "settings.json").write_text(
            json.dumps({"qwen_model_format": "official", "qwen_device": "cuda"}),
            encoding="utf-8",
        )
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.qwen_model_format == "official"
        assert loaded.qwen_device == "cuda"

        for fmt in ("official", "gguf"):
            save_settings(Settings(qwen_model_format=fmt), data_dir=tmp_path)
            assert load_settings(data_dir=tmp_path).qwen_model_format == fmt
        for quant in ("Q8_0", "Q4_K_M"):
            save_settings(Settings(qwen_gguf_quantization=quant), data_dir=tmp_path)
            assert load_settings(data_dir=tmp_path).qwen_gguf_quantization == quant
        for device in ("auto", "cpu", "cuda", "metal"):
            save_settings(Settings(qwen_gguf_device=device), data_dir=tmp_path)
            assert load_settings(data_dir=tmp_path).qwen_gguf_device == device

    def test_variant_field_clamping(self, tmp_path: Path, caplog) -> None:
        # Picking GGUF must not erase the remembered official device, and
        # switching back must not erase the GGUF one.
        save_settings(
            Settings(
                qwen_device="cuda",
                qwen_model_format="gguf",
                qwen_gguf_device="metal",
                qwen_gguf_quantization="Q4_K_M",
            ),
            data_dir=tmp_path,
        )
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.qwen_device == "cuda"
        assert loaded.qwen_gguf_device == "metal"
        assert loaded.qwen_gguf_quantization == "Q4_K_M"

        (tmp_path / "settings.json").write_text(
            json.dumps(
                {
                    "qwen_model_format": "onnx",
                    "qwen_gguf_quantization": "F16",
                    "qwen_gguf_device": "mps",
                    "qwen_device": "cuda",
                    "output_dir": "/tmp/keep",
                }
            ),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded.qwen_model_format == "gguf"
        assert loaded.qwen_gguf_quantization == "Q8_0"
        assert loaded.qwen_gguf_device == "auto"
        # The official-weights device is a different field — it survives.
        assert loaded.qwen_device == "cuda"
        assert loaded.output_dir == "/tmp/keep"
        assert any("qwen_model_format" in r.message for r in caplog.records)
        assert any("qwen_gguf_quantization" in r.message for r in caplog.records)
        assert any("qwen_gguf_device" in r.message for r in caplog.records)

        # Native Metal is a legal GGUF device; PyTorch's "mps" is not, and
        # clamping it must not touch the official device field.
        (tmp_path / "settings.json").write_text(
            json.dumps({"qwen_gguf_device": "metal", "qwen_device": "mps"}),
            encoding="utf-8",
        )
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.qwen_gguf_device == "metal"
        assert loaded.qwen_device == "mps"


class TestOrtKnobs:
    """ORT/BLAS tuning knobs (perf track 7.1): defaults = today's behavior."""

    def test_defaults_are_the_sdk_defaults(self) -> None:
        settings = Settings()
        assert settings.ort_intra_op_threads is None
        assert settings.ort_step_single_thread is False
        assert settings.ort_spin_during_job is False
        assert settings.blas_threads is None

    def test_knobs_round_trip_and_validate(self, tmp_path: Path) -> None:
        tuned = Settings(
            ort_intra_op_threads=4,
            ort_step_single_thread=True,
            ort_spin_during_job=True,
            blas_threads=1,
        )
        save_settings(tuned, data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path) == tuned
        for bad in (
            {"ort_intra_op_threads": -1},
            {"ort_intra_op_threads": True},
            {"ort_step_single_thread": "yes"},
            {"ort_spin_during_job": 1},
            {"blas_threads": 0},
        ):
            with pytest.raises(ValueError):
                Settings(**bad)
