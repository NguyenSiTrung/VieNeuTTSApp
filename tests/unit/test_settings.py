"""Settings persistence: JSON round-trip, defaults, graceful corruption handling."""

import json
import logging
import os
import stat
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

        original = Settings(window_x=120, window_y=64, window_width=1280, window_height=800)
        save_settings(original, data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path) == original
        maximized = replace(original, window_maximized=True)
        save_settings(maximized, data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path).window_maximized is True

        # Recent voices: per-profile MRU lists round-trip (lists normalize to
        # tuples so the loaded copy compares equal).
        recents = replace(
            original,
            recent_voices={"vieneu": ["Hà Vy", "Minh Đức"], "qwen_custom_0_6b": ("Vivian",)},
        )
        assert recents.recent_voices == {
            "vieneu": ("Hà Vy", "Minh Đức"),
            "qwen_custom_0_6b": ("Vivian",),
        }
        save_settings(recents, data_dir=tmp_path)
        assert load_settings(data_dir=tmp_path) == recents
        on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert on_disk["recent_voices"]["vieneu"] == ["Hà Vy", "Minh Đức"]

    def test_legacy_and_invalid_files(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Settings(window_x=1.5)
        with pytest.raises(ValueError):
            Settings(window_width=True)
        with pytest.raises(ValueError):
            Settings(window_maximized="yes")
        with pytest.raises(ValueError):
            Settings(live_preview="yes")

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
        assert loaded.recent_voices == {}  # pre-recents file: no field at all


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

        (tmp_path / "settings.json").write_text(json.dumps({"language": "fr"}), encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded == Settings()
        assert caplog.records


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

        # Garbage recent voices degrade field by field: unknown profiles,
        # non-list values, blank/non-string ids, duplicates and overflow are
        # dropped; the rest of the file survives and a warning is logged.
        caplog.clear()
        (tmp_path / "settings.json").write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "recent_voices": {
                        "vieneu": ["A", " ", 7, "B", "A", None, "C", "D"],
                        "qwen_custom_0_6b": "Vivian",
                        "qwen_9b": ["X"],
                        "qwen_base_0_6b": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_settings(data_dir=tmp_path)
        assert loaded.theme == "dark"
        assert loaded.recent_voices == {"vieneu": ("A", "B", "C")}
        assert any("recent_voices" in r.message for r in caplog.records)
        for garbage in ("Vivian", ["A"], 3, None, {"vieneu": {"a": 1}}):
            (tmp_path / "settings.json").write_text(
                json.dumps({"output_dir": "/tmp/keep", "recent_voices": garbage}),
                encoding="utf-8",
            )
            loaded = load_settings(data_dir=tmp_path)
            assert loaded.output_dir == "/tmp/keep", garbage
            assert loaded.recent_voices == {}, garbage
        # A clean list loads silently.
        caplog.clear()
        (tmp_path / "settings.json").write_text(
            json.dumps({"recent_voices": {"vieneu": ["A", "B"]}}), encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING):
            assert load_settings(data_dir=tmp_path).recent_voices == {"vieneu": ("A", "B")}
        assert not any("recent_voices" in r.message for r in caplog.records)


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


class TestAiDubbingSettings:
    """ASR, Diarization, and LLM provider settings persist and clamp safely."""

    def test_ai_dubbing_settings_round_trip_and_clamp(self, tmp_path: Path, caplog) -> None:
        custom = Settings(
            asr_backend="faster_whisper",
            asr_model="small",
            asr_language="vi",
            asr_device="cpu",
            asr_threads=8,
            diarization_backend="nemotron_gguf",
            diarization_model="nemotron-3-diarization-bf16",
            diarization_max_speakers=4,
            diarization_device="vulkan",
            diarization_threads=8,
            subtitle_export_speaker_labels=True,
            llm_base_url="http://localhost:11434/v1",
            llm_api_key="local-dev-key",
            llm_model="qwen2.5:14b",
            asr_api_base_url="https://asr.example/v1",
            asr_api_key="asr-secret",
            asr_api_model="whisper-1",
            asr_cloud_fallback=True,
            llm_model_refine="refine",
            llm_model_translate="translate",
            llm_model_fit="fit",
            llm_concurrency=4,
            llm_timeout_s=180,
            llm_batch_size=40,
        )
        save_settings(custom, data_dir=tmp_path)
        loaded = load_settings(data_dir=tmp_path)
        assert loaded == custom
        assert loaded.asr_cloud_fallback is True
        assert loaded.asr_backend == "faster_whisper"
        assert loaded.asr_model == "small"
        assert loaded.asr_language == "vi"
        assert loaded.asr_device == "cpu"
        assert loaded.asr_threads == 8
        assert loaded.diarization_backend == "nemotron_gguf"
        assert loaded.diarization_max_speakers == 4
        assert loaded.diarization_device == "vulkan"
        assert loaded.diarization_threads == 8
        assert loaded.subtitle_export_speaker_labels is True
        assert loaded.llm_base_url == "http://localhost:11434/v1"
        assert loaded.llm_api_key == "local-dev-key"
        assert loaded.llm_model == "qwen2.5:14b"

        for bad in (
            {"asr_backend": "invalid"},
            {"asr_device": "tpu"},
            {"asr_threads": -1},
            {"asr_threads": 65},
            {"asr_threads": True},
            {"diarization_backend": "invalid"},
            {"diarization_backend": "nemo"},
            {"diarization_max_speakers": -1},
            {"diarization_max_speakers": 99},
            {"diarization_device": "tpu"},
            {"diarization_threads": -1},
            {"diarization_threads": 65},
            {"diarization_threads": True},
            {"subtitle_export_speaker_labels": "yes"},
            {"llm_concurrency": -1},
            {"llm_concurrency": 9},
            {"llm_concurrency": True},
            {"llm_timeout_s": 14},
            {"llm_timeout_s": 601},
            {"llm_timeout_s": 15.0},
            {"llm_batch_size": 4},
            {"llm_batch_size": 61},
            {"llm_batch_size": False},
            {"asr_api_base_url": None},
            {"asr_api_key": 5},
            {"asr_api_model": []},
            {"llm_model_refine": 1},
            {"llm_model_translate": None},
            {"llm_model_fit": False},
            # bool rule in the other direction: an int is not a bool flag.
            {"asr_cloud_fallback": 1},
            {"asr_cloud_fallback": "yes"},
        ):
            with pytest.raises(ValueError):
                Settings(**bad)

        (tmp_path / "settings.json").write_text(
            json.dumps(
                {
                    "asr_backend": "unknown_asr",
                    "asr_device": "tpu",
                    "asr_threads": 999,
                    "diarization_backend": "unknown_diar",
                    "diarization_max_speakers": 999,
                    "diarization_device": "tpu",
                    "diarization_threads": 999,
                    "subtitle_export_speaker_labels": "yes",
                    "llm_batch_size": 999,
                    "llm_concurrency": True,
                    "llm_timeout_s": 10,
                    "asr_api_key": 5,
                    "llm_api_key": "must-not-be-logged",
                    "llm_model_fit": None,
                    "asr_cloud_fallback": 1,
                    "asr_model": "base",
                    "output_dir": "/tmp/survives",
                }
            ),
            encoding="utf-8",
        )
        clamped = load_settings(data_dir=tmp_path)
        assert clamped.asr_backend == "auto"
        assert clamped.asr_device == "auto"
        assert clamped.asr_threads == 0
        assert clamped.diarization_backend == "auto"
        assert clamped.diarization_max_speakers == 0
        assert clamped.diarization_device == "auto"
        assert clamped.diarization_threads == 0
        assert clamped.subtitle_export_speaker_labels is False
        assert clamped.llm_batch_size == 25
        assert clamped.llm_concurrency == 0
        assert clamped.llm_timeout_s == 120
        assert clamped.asr_api_key == ""
        assert clamped.llm_api_key == "must-not-be-logged"
        assert clamped.llm_model_fit == ""
        assert "must-not-be-logged" not in caplog.text
        assert clamped.output_dir == "/tmp/survives"
        assert clamped.asr_cloud_fallback is False
        # A stored model choice survives the large-v3-turbo default (FR-3.3).
        assert clamped.asr_model == "base"

        # Every stored diarization device keeps loading; "cuda" (the Windows
        # audio.cpp CUDA builds) joins the vocabulary.
        for device in ("auto", "cpu", "vulkan", "metal", "cuda"):
            (tmp_path / "settings.json").write_text(
                json.dumps({"diarization_device": device, "asr_cloud_fallback": True}),
                encoding="utf-8",
            )
            stored = load_settings(data_dir=tmp_path)
            assert (stored.diarization_device, stored.asr_cloud_fallback) == (device, True)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
    def test_settings_permissions_are_owner_only(self, tmp_path: Path) -> None:
        previous_umask = os.umask(0)
        try:
            path = save_settings(Settings(llm_api_key="secret"), data_dir=tmp_path)
        finally:
            os.umask(previous_umask)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o644)
        assert load_settings(data_dir=tmp_path).llm_api_key == "secret"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_legacy_nemo_backend_is_migrated(self, tmp_path: Path) -> None:
        (tmp_path / "settings.json").write_text(
            json.dumps({"diarization_backend": "nemo"}), encoding="utf-8"
        )
        assert load_settings(data_dir=tmp_path).diarization_backend == "nemotron_gguf"

    def test_controller_asr_device_threads_runtime_and_throttled_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qcoreapp
    ) -> None:
        import vienetts_app.core.ai_models as ai_models
        from vienetts_app.ui.bg_ops import run_sync
        from vienetts_app.ui.controller import AppController

        ctrl = AppController(
            data_dir=tmp_path,
            bg_runner=run_sync,
        )
        try:
            # The default is the catalog's large-v3-turbo (Q5_0 file), and the controller
            # exposes it as-is rather than normalizing an unknown id to "base".
            assert Settings().asr_model == "large-v3-turbo"
            assert ai_models.WHISPER_GGUF_MODELS["large-v3-turbo"].filename == (
                "ggml-large-v3-turbo-q5_0.bin"
            )
            assert ctrl.asrModel == "large-v3-turbo"
            assert ctrl.asrDevice == "auto"
            assert ctrl.asrThreads == 0
            ctrl.asrDevice = "cpu"
            ctrl.asrThreads = 12
            assert ctrl.asrDevice == "cpu"
            assert ctrl.asrThreads == 12

            # Unknown asrModel ID normalizes to "base"
            ctrl.asrModel = "unknown-id"
            assert ctrl.asrModel == "base"

            assert ctrl.whisperCppInstalled is False
            assert "cpu" in ctrl.whisperCppAvailableVariants

            counts = {"install": 0, "progress": 0}

            def nonlocal_inc(key: str) -> None:
                counts[key] += 1

            ctrl.aiModelsInstallChanged.connect(lambda: nonlocal_inc("install"))
            ctrl.aiDownloadProgressChanged.connect(lambda: nonlocal_inc("progress"))

            def _fake_download_asr(model_id: str, data_dir: Path, *, progress_cb=None, **_kw):
                if progress_cb is not None:
                    for step in range(1, 501):
                        progress_cb(step / 500.0)
                out = data_dir / "models" / "whisper" / "ggml-base.bin"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"ok")
                return out

            monkeypatch.setattr(ai_models, "download_asr_model", _fake_download_asr)
            monkeypatch.setattr(ai_models, "is_whisper_cpp_installed", lambda _d=None: True)
            counts["install"] = 0
            counts["progress"] = 0
            assert ctrl.downloadAsrModel() is True
            # Only start + finish emit aiModelsInstallChanged (2 total), never 500!
            assert counts["install"] == 2
            assert 10 <= counts["progress"] <= 105
        finally:
            ctrl.shutdown()
