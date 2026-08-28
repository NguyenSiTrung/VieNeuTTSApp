"""TTSEngine: lazy single-instance ownership, SDK wrappers, error propagation."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core.engine import (
    DEFAULT_MAX_CHARS,
    MODELS_MISSING_MARKER,
    ModelsMissingError,
    TTSEngine,
    TTSEngineError,
    is_models_missing,
    preset_voices,
    saved_voice_names,
    split_text_for_streaming,
)


def silent(samples: int = 48_000) -> np.ndarray:
    return np.zeros(samples, dtype=np.float32)


class FakeVieneu:
    """Fake of the confirmed SDK surface (docs/spike-report.md §0)."""

    instances: list["FakeVieneu"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.backend = "onnx"
        self.sample_rate = 48_000
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Mirrors vieneu 3.3.0 V3TurboVieNeuTTS._preset_voices (dict name →
        # {"description","gender","style","speaker_emb","codes"}).
        self._preset_voices: dict[str, dict[str, Any]] = {
            "Adam": {
                "description": "Nam · Bắc · Phong cách tin tức",
                "gender": "male",
                "style": "tin_tuc",
                "speaker_emb": np.zeros(4, dtype=np.float32),
                "codes": np.zeros(2, dtype=np.int64),
            }
        }
        self.saved_to: list[str] = []
        FakeVieneu.instances.append(self)

    def infer(
        self,
        text,
        voice=None,
        ref_audio=None,
        temperature=None,
        top_k=None,
        show_progress=True,
        **kw,
    ) -> np.ndarray:
        self.calls.append(
            (
                "infer",
                {
                    "text": text,
                    "voice": voice,
                    "ref_audio": ref_audio,
                    "temperature": temperature,
                    "show_progress": show_progress,
                },
            )
        )
        return silent(2400)

    def infer_stream(self, text, voice=None, **kw) -> Iterator[np.ndarray]:
        self.calls.append(("infer_stream", {"text": text, "voice": voice}))
        yield silent(15360)
        yield silent(23040)

    def infer_batch(self, texts, voice=None, **kw) -> list[np.ndarray]:
        self.calls.append(("infer_batch", {"texts": list(texts), "voice": voice}))
        return [silent(1000) for _ in texts]

    def list_preset_voices(self) -> list[tuple[str, str]]:
        return [("Label — Nam · Bắc", "Adam")]

    def add_voice(self, name, ref_audio, *, denoise=True, save=False, **kw) -> str:
        self.calls.append(("add_voice", {"name": name, "denoise": denoise, "save": save}))
        return name

    def remove_voice(self, name, *, save=False, **kw) -> None:
        self.calls.append(("remove_voice", {"name": name, "save": save}))

    def denoise(self, ref_audio, out_path=None, max_seconds=None):
        self.calls.append(("denoise", {"ref_audio": ref_audio}))
        return silent(44100), 44_100

    def save(self, audio, output_path) -> None:
        self.calls.append(("save", {"samples": len(audio), "path": str(output_path)}))

    def save_voices(self, path=None) -> str:
        # Mirrors vieneu 3.3.0: writes the current voices as JSON to `path`.
        self.calls.append(("save_voices", {"path": str(path)}))
        self.saved_to.append(str(path))
        Path(path).write_text(
            json.dumps(
                {
                    "meta": {"note": "fake"},
                    "default_voice": "Adam",
                    "presets": {
                        n: {
                            "description": v.get("description", ""),
                            "gender": v.get("gender", ""),
                            "style": v.get("style", ""),
                            "speaker_emb": v.get("speaker_emb").tolist()
                            if v.get("speaker_emb") is not None
                            else None,
                            "codes": v.get("codes").tolist()
                            if v.get("codes") is not None
                            else None,
                        }
                        for n, v in self._preset_voices.items()
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return str(path)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeVieneu.instances = []
    yield
    FakeVieneu.instances = []


def make_engine(**kwargs: Any) -> TTSEngine:
    if "factory" not in kwargs:
        kwargs["factory"] = lambda **kw: FakeVieneu(**kw)
    return TTSEngine(**kwargs)


class TestLazyInit:
    def test_factory_not_called_until_first_request(self) -> None:
        engine = make_engine()
        assert engine.is_initialized is False
        assert FakeVieneu.instances == []
        engine.infer("Xin chào")
        assert engine.is_initialized is True
        assert len(FakeVieneu.instances) == 1

    def test_initialized_once_across_many_requests(self) -> None:
        engine = make_engine()
        engine.infer("a")
        engine.infer_batch(["a", "b"])
        list(engine.infer_stream("c"))
        assert len(FakeVieneu.instances) == 1

    def test_explicit_initialize_is_idempotent(self) -> None:
        engine = make_engine()

        engine.initialize()
        engine.initialize()

        assert len(FakeVieneu.instances) == 1

    def test_init_kwargs_forwarded(self) -> None:
        engine = make_engine(backend="onnx", precision="int8")
        engine.infer("hi")
        assert FakeVieneu.instances[0].init_kwargs == {"backend": "onnx", "precision": "int8"}

    def test_optional_tuning_kwargs_are_forwarded(self) -> None:
        engine = make_engine(threads=4, max_batch_size=8)

        engine.initialize()

        assert FakeVieneu.instances[0].init_kwargs == {
            "backend": "auto",
            "precision": "int8",
            "threads": 4,
            "max_batch_size": 8,
        }

    def test_optional_tuning_kwargs_validate_bounds(self) -> None:
        with pytest.raises(ValueError, match="threads"):
            TTSEngine(threads=-1)
        with pytest.raises(ValueError, match="max_batch_size"):
            TTSEngine(max_batch_size=0)

    def test_sample_rate_available_after_init(self) -> None:
        engine = make_engine()
        with pytest.raises(TTSEngineError, match="not initialized"):
            _ = engine.sample_rate
        engine.infer("hi")
        assert engine.sample_rate == 48_000

    def test_close_resets_lazy_state(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        first = FakeVieneu.instances[0]
        engine.close()
        assert first.closed is True
        assert engine.is_initialized is False
        engine.infer("hi again")
        assert len(FakeVieneu.instances) == 2


class TestWrappers:
    def test_infer_passes_voice_and_progress_off(self) -> None:
        engine = make_engine()
        audio = engine.infer("Xin chào", voice="Adam")
        assert audio.dtype == np.float32 and len(audio) == 2400
        op, kwargs = FakeVieneu.instances[0].calls[0]
        assert op == "infer"
        assert kwargs["voice"] == "Adam"
        assert kwargs["show_progress"] is False

    def test_infer_forwards_optional_sampling_params(self) -> None:
        engine = make_engine()
        engine.infer("hi", temperature=0.7, top_k=25)
        kwargs = FakeVieneu.instances[0].calls[0][1]
        assert kwargs["temperature"] == 0.7

    def test_infer_omits_none_temperature(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        assert FakeVieneu.instances[0].calls[0][1]["temperature"] is None

    def test_infer_stream_yields_chunks(self) -> None:
        engine = make_engine()
        chunks = list(engine.infer_stream("text", voice="Adam"))
        assert [len(c) for c in chunks] == [15360, 23040]
        assert all(c.dtype == np.float32 for c in chunks)

    def test_infer_batch_returns_list(self) -> None:
        engine = make_engine()
        wavs = engine.infer_batch(["a", "b", "c"], voice="Adam")
        assert len(wavs) == 3

    def test_add_voice_round_trip(self) -> None:
        engine = make_engine()
        assert engine.add_voice("my", "/tmp/ref.wav", denoise=False, save=True) == "my"
        kwargs = FakeVieneu.instances[0].calls[0][1]
        assert kwargs == {"name": "my", "denoise": False, "save": True}

    def test_list_voices_delegates(self) -> None:
        engine = make_engine()
        assert engine.list_voices() == [("Label — Nam · Bắc", "Adam")]

    def test_denoise_returns_tuple(self) -> None:
        engine = make_engine()
        wav, sr = engine.denoise("/tmp/clip.wav")
        assert sr == 44_100 and wav.dtype == np.float32

    def test_save_delegates(self, tmp_path) -> None:
        engine = make_engine()
        engine.save(silent(100), tmp_path / "o.wav")
        kwargs = FakeVieneu.instances[0].calls[0][1]
        assert kwargs["samples"] == 100
        assert kwargs["path"].endswith("o.wav")


class TestErrorPropagation:
    def test_torch_missing_becomes_actionable_error(self) -> None:
        def factory(**kw: Any):
            raise ModuleNotFoundError("No module named 'torch'")

        engine = TTSEngine(factory=factory, backend="torch")
        with pytest.raises(TTSEngineError, match="onnx|gpu"):
            engine.infer("hi")

    def test_sdk_errors_wrapped_with_cause(self) -> None:
        class Boom(FakeVieneu):
            def infer(self, text, **kw):
                raise ValueError("Voice 'Nope' not found. Available: ['Adam']")

        engine = TTSEngine(factory=lambda **kw: Boom(**kw))
        with pytest.raises(TTSEngineError, match="Nope") as excinfo:
            engine.infer("hi", voice="Nope")
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_backend_attribute_after_init(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        assert engine.backend == "onnx"


class TestModelsMissingClassification:
    """_ensure() classifies weights-missing factory failures (FR-4.6c core).

    Exception shapes verified against vieneu 3.3.0 + huggingface_hub 1.28.0
    (live repro: empty HF_HOME + HF_HUB_OFFLINE=1 through the real Vieneu(...)
    factory): OnnxV3LiteEngine._fetch() → hf_hub_download() raises
    ``huggingface_hub.errors.LocalEntryNotFoundError``, whose MRO is
    LocalEntryNotFoundError → FileNotFoundError → OSError.
    """

    def test_hf_local_entry_not_found_raises_models_missing(self) -> None:
        # The REAL offline/missing-cache shape: LocalEntryNotFoundError is a
        # FileNotFoundError/OSError subclass raised by hf_hub_download.
        huggingface_hub = pytest.importorskip("huggingface_hub")
        real_exc = huggingface_hub.errors.LocalEntryNotFoundError(
            "Cannot find the requested files in the disk cache and outgoing traffic "
            "has been disabled."
        )

        def factory(**kw: Any):
            raise real_exc

        engine = TTSEngine(factory=factory)
        with pytest.raises(ModelsMissingError) as excinfo:
            engine.infer("hi")
        text = str(excinfo.value)
        assert MODELS_MISSING_MARKER in text
        assert "scripts/fetch_models.py" in text

    def test_filenotfounderror_on_cache_path_raises_models_missing(self) -> None:
        def factory(**kw: Any):
            raise FileNotFoundError(
                "[Errno 2] No such file or directory: "
                "'/home/user/.cache/huggingface/hub/models--pnnbao-ump--VieNeu-TTS-v3-Turbo/"
                "snapshots/abc/onnx_int8/vieneu_prefill.onnx'"
            )

        engine = TTSEngine(factory=factory)
        with pytest.raises(ModelsMissingError, match="fetch_models") as excinfo:
            engine.list_voices()
        assert MODELS_MISSING_MARKER in str(excinfo.value)

    def test_hf_offline_mode_error_shape_raises_models_missing(self) -> None:
        # OfflineModeIsEnabled subclasses ConnectionError (an OSError), NOT
        # FileNotFoundError — verified in huggingface_hub/errors.py; when the
        # SDK surfaces it (local_files_only path), it is still weights-missing.
        huggingface_hub = pytest.importorskip("huggingface_hub")
        offline = huggingface_hub.errors.OfflineModeIsEnabled(
            "Cannot access file since 'local_files_only=True' as been set."
        )

        def factory(**kw: Any):
            raise offline

        engine = TTSEngine(factory=factory)
        with pytest.raises(ModelsMissingError) as excinfo:
            list(engine.infer_stream("hi"))
        assert MODELS_MISSING_MARKER in str(excinfo.value)

    def test_generic_runtime_error_stays_plain_tts_engine_error(self) -> None:
        def factory(**kw: Any):
            raise RuntimeError("kaboom")

        engine = TTSEngine(factory=factory)
        with pytest.raises(TTSEngineError) as excinfo:
            engine.infer("hi")
        assert not isinstance(excinfo.value, ModelsMissingError)
        assert "kaboom" in str(excinfo.value)

    def test_torch_missing_branch_unchanged(self) -> None:
        # Regression guard for the existing torch ModuleNotFoundError policy.
        def factory(**kw: Any):
            raise ModuleNotFoundError("No module named 'torch'")

        engine = TTSEngine(factory=factory, backend="torch")
        with pytest.raises(TTSEngineError, match="onnx|gpu") as excinfo:
            engine.infer("hi")
        assert not isinstance(excinfo.value, ModelsMissingError)

    def test_infer_stream_factory_raise_classifies(self) -> None:
        # infer_stream routes through _ensure(), so a models-missing raise
        # during synthesis also classifies correctly.
        def factory(**kw: Any):
            raise FileNotFoundError("No such file or directory: 'hf-cache/snapshots/x.onnx'")

        engine = TTSEngine(factory=factory)
        with pytest.raises(ModelsMissingError):
            next(engine.infer_stream("hi"))

    def test_models_missing_is_tts_engine_error(self) -> None:
        # Worker catch path keeps working: it catches TTSEngineError.
        assert issubclass(ModelsMissingError, TTSEngineError)

    def test_is_models_missing_helper(self) -> None:
        err = ModelsMissingError(f"{MODELS_MISSING_MARKER}: run scripts/fetch_models.py")
        assert is_models_missing(str(err)) is True
        generic = TTSEngineError("Engine initialization failed: kaboom")
        assert is_models_missing(str(generic)) is False
        assert is_models_missing("") is False


def write_asset(path: Path, presets: dict[str, Any], default_voice: str = "Adam") -> Path:
    payload = {
        "meta": {"note": "test asset", "count": len(presets)},
        "default_voice": default_voice,
        "presets": presets,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestPresetVoicesCatalog:
    """Module-level preset_voices(): catalog WITHOUT initializing any model."""

    def test_reads_injected_asset_without_model(self, tmp_path: Path) -> None:
        asset = write_asset(
            tmp_path / "voices.json",
            {
                "Minh Đức": {
                    "description": "Nam · Bắc · Phong cách tin tức",
                    "gender": "male",
                    "style": "tin_tuc",
                    "speaker_emb": [0.1, 0.2],
                    "codes": [1, 2, 3],
                }
            },
        )
        catalog = preset_voices(asset)
        assert catalog == [
            {
                "name": "Minh Đức",
                "description": "Nam · Bắc · Phong cách tin tức",
                "gender": "male",
                "style": "tin_tuc",
            }
        ]

    def test_missing_fields_default_to_empty_strings(self, tmp_path: Path) -> None:
        asset = write_asset(tmp_path / "voices.json", {"Bare": {"description": "d"}})
        entry = preset_voices(asset)[0]
        assert entry == {"name": "Bare", "description": "d", "gender": "", "style": ""}

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        assert preset_voices(tmp_path / "nope.json") == []

    def test_corrupt_json_returns_empty_list(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert preset_voices(bad) == []

    def test_non_dict_payload_returns_empty_list(self, tmp_path: Path) -> None:
        odd = tmp_path / "odd.json"
        odd.write_text(json.dumps([1, 2]), encoding="utf-8")
        assert preset_voices(odd) == []

    def test_default_asset_resolves_from_installed_vieneu(self) -> None:
        # The real vieneu 3.3.0 asset ships 20 curated presets; reading it must
        # stay model-free (no Vieneu() call, just JSON).
        import vieneu

        catalog = preset_voices()
        expected = Path(vieneu.__file__).parent / "assets" / "voices_v3_turbo.json"
        assert expected.is_file()
        assert len(catalog) == 20
        assert catalog[0].keys() == {"name", "description", "gender", "style"}


class TestVoicesDirMergeBack:
    """_ensure() re-injects persisted cloned voices into tts._preset_voices."""

    def _persisted(self, tmp_path: Path, presets: dict[str, Any]) -> Path:
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        write_asset(voices_dir / "voices.json", presets)
        return voices_dir

    def test_persisted_cloned_voice_injected_after_factory(self, tmp_path: Path) -> None:
        voices_dir = self._persisted(
            tmp_path,
            {
                "MyClone": {
                    "description": "",
                    "gender": "",
                    "style": "",
                    "speaker_emb": [0.5, -0.5],
                    "codes": [7, 8],
                }
            },
        )
        engine = make_engine(voices_dir=voices_dir)
        engine.infer("hi")  # triggers _ensure()
        tts = FakeVieneu.instances[0]
        assert "MyClone" in tts._preset_voices
        injected = tts._preset_voices["MyClone"]
        assert injected["speaker_emb"].dtype == np.float32
        assert injected["speaker_emb"].tolist() == [0.5, -0.5]
        assert injected["codes"].dtype == np.int64
        assert injected["codes"].tolist() == [7, 8]

    def test_preset_name_not_overwritten(self, tmp_path: Path) -> None:
        # "Adam" is already in the fake's presets — persisted entry must NOT
        # clobber the live SDK entry.
        voices_dir = self._persisted(
            tmp_path,
            {
                "Adam": {
                    "description": "evil override",
                    "speaker_emb": [9.9],
                    "codes": [1],
                }
            },
        )
        engine = make_engine(voices_dir=voices_dir)
        engine.infer("hi")
        assert FakeVieneu.instances[0]._preset_voices["Adam"]["description"] != "evil override"

    def test_missing_emb_or_codes_become_none(self, tmp_path: Path) -> None:
        voices_dir = self._persisted(tmp_path, {"NoEmb": {"description": "x"}})
        engine = make_engine(voices_dir=voices_dir)
        engine.infer("hi")
        injected = FakeVieneu.instances[0]._preset_voices["NoEmb"]
        assert injected["speaker_emb"] is None
        assert injected["codes"] is None

    def test_corrupt_persisted_file_does_not_break_init(self, tmp_path: Path) -> None:
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        (voices_dir / "voices.json").write_text("][ broken", encoding="utf-8")
        engine = make_engine(voices_dir=voices_dir)
        audio = engine.infer("hi")  # must not raise
        assert len(audio) == 2400

    def test_missing_persisted_file_is_fine(self, tmp_path: Path) -> None:
        engine = make_engine(voices_dir=tmp_path / "voices")  # dir doesn't even exist
        engine.infer("hi")
        assert len(FakeVieneu.instances[0]._preset_voices) == 1

    def test_no_voices_dir_skips_merge(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        assert len(FakeVieneu.instances[0]._preset_voices) == 1

    def test_merge_only_runs_on_first_init(self, tmp_path: Path) -> None:
        voices_dir = self._persisted(tmp_path, {"Clone1": {"speaker_emb": [0.1]}})
        engine = make_engine(voices_dir=voices_dir)
        engine.infer("hi")
        engine.close()
        engine.infer("again")
        second = FakeVieneu.instances[1]
        assert "Clone1" in second._preset_voices  # re-merged on re-init


class TestPersistVoices:
    def test_requires_initialized_engine(self, tmp_path: Path) -> None:
        engine = make_engine(voices_dir=tmp_path / "voices")
        with pytest.raises(TTSEngineError, match="not initialized"):
            engine.persist_voices()

    def test_saves_into_voices_dir_and_returns_path(self, tmp_path: Path) -> None:
        voices_dir = tmp_path / "voices"  # deliberately NOT created yet
        engine = make_engine(voices_dir=voices_dir)
        engine.infer("hi")
        path = engine.persist_voices()
        assert path == voices_dir / "voices.json"
        assert path.is_file()
        tts = FakeVieneu.instances[0]
        op, kwargs = tts.calls[-1]
        assert op == "save_voices"
        assert kwargs["path"] == str(path)

    def test_persist_without_voices_dir_raises(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        with pytest.raises(TTSEngineError, match="voices_dir"):
            engine.persist_voices()


class TestSavedVoiceNames:
    def test_excludes_sdk_preset_names(self, tmp_path: Path) -> None:
        asset = write_asset(
            tmp_path / "asset.json",
            {"Adam": {"description": "preset"}, "Minh Đức": {"description": "preset2"}},
        )
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        write_asset(
            voices_dir / "voices.json",
            {
                "Adam": {"description": "also a preset"},
                "MyClone": {"description": "", "speaker_emb": [0.1]},
            },
        )
        names = saved_voice_names(voices_dir, asset_path=asset)
        assert names == ["MyClone"]

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert saved_voice_names(tmp_path / "nope") == []

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        (voices_dir / "voices.json").write_text("~~~", encoding="utf-8")
        assert saved_voice_names(voices_dir) == []

    def test_order_preserved(self, tmp_path: Path) -> None:
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        write_asset(
            voices_dir / "voices.json",
            {"Zeta": {}, "Alpha": {}, "Mid": {}},
        )
        assert saved_voice_names(voices_dir) == ["Zeta", "Alpha", "Mid"]


def _sentence(prefix: str, width: int) -> str:
    """A deterministic sentence of exactly ``width`` chars ending in a period."""
    body = prefix + "a" * max(0, width - len(prefix) - 1)
    return body[: width - 1] + "."


class TestSplitTextForStreaming:
    """Pure segmentation helper for chunked stream dispatch (FR-4.6d)."""

    def test_default_constant_is_sensible(self) -> None:
        # Rationale documented on DEFAULT_MAX_CHARS: must be >= the SDK's own
        # internal 256-char AR chunk so app-level segments add no extra prefill.
        assert DEFAULT_MAX_CHARS == 512

    def test_short_text_passes_through_unchanged(self) -> None:
        # Single-segment equivalence: text at/below the limit is returned
        # byte-for-byte, so today's infer_stream call is identical.
        text = "Xin chào Việt Nam!"
        assert split_text_for_streaming(text) == [text]

    def test_empty_and_whitespace_only_return_no_segments(self) -> None:
        assert split_text_for_streaming("") == []
        assert split_text_for_streaming("   \n\t \u00a0 ") == []

    def test_sentences_packed_within_max_chars(self) -> None:
        s1 = _sentence("First", 60)
        s2 = _sentence("Second", 60)
        s3 = _sentence("Third", 60)
        segments = split_text_for_streaming(f"{s1} {s2} {s3}", max_chars=140)
        # Greedy pack: 60 + 1 + 60 = 121 <= 140; adding s3 would exceed it.
        assert segments == [f"{s1} {s2}", s3]

    def test_honors_max_chars_cap(self) -> None:
        text = " ".join(_sentence(f"S{i}", 50) for i in range(10))
        for segment in split_text_for_streaming(text, max_chars=120):
            assert len(segment) <= 120

    def test_sentence_boundaries_kept_intact(self) -> None:
        sentences = [
            "Hà Nội là thủ đô của Việt Nam.",
            "Sài Gòn sầm uất về đêm!",
            "Mai trời mưa nhé?",
            "Ông lão câu cá bên sông hồng…",
        ]
        text = " ".join(sentences)
        segments = split_text_for_streaming(text, max_chars=60)
        # Every sentence survives intact inside exactly one segment; no
        # segment splits mid-sentence when the unit itself fits the cap.
        reconstructed: list[str] = []
        for sentence in sentences:
            for segment in segments:
                if sentence in segment:
                    reconstructed.append(sentence)
                    break
            else:
                pytest.fail(f"sentence lost: {sentence!r}")
        assert sorted(reconstructed) == sorted(sentences)

    def test_newlines_are_boundaries_not_content(self) -> None:
        text = "Đoạn một có nội dung.\nĐoạn hai theo sau.\n\nĐoạn ba kết thúc."
        segments = split_text_for_streaming(text, max_chars=200)
        joined = " ".join(segments)
        assert "\n" not in joined
        for fragment in ("Đoạn một có nội dung.", "Đoạn hai theo sau.", "Đoạn ba kết thúc."):
            assert fragment in joined

    def test_oversized_unit_hard_split_at_max_chars(self) -> None:
        run = "z" * 1200
        segments = split_text_for_streaming(run, max_chars=500)
        assert all(len(s) <= 500 for s in segments)
        assert "".join(segments) == run

    def test_hard_split_prefers_space_break(self) -> None:
        unit = "x" * 300 + " " + "y" * 300
        segments = split_text_for_streaming(unit, max_chars=500)
        assert segments == ["x" * 300, "y" * 300]

    def test_oversized_unit_among_normal_sentences(self) -> None:
        s1 = _sentence("Open", 40)
        giant = "w" * 700
        s2 = _sentence("Close", 40)
        segments = split_text_for_streaming(f"{s1} {giant} {s2}", max_chars=200)
        assert all(len(s) <= 200 for s in segments)
        assert any(giant.startswith(s.rstrip()) and s for s in segments[:2])
        assert segments[-1].endswith(s2)

    def test_unicode_vietnamese_diacritics_safe(self) -> None:
        text = (
            "Tiếng Việt là ngôn ngữ quốc gia của Việt Nam. "
            "Chữ Quốc ngữ dùng nhiều dấu thanh khác nhau!"
        )
        segments = split_text_for_streaming(text, max_chars=40)
        joined = "".join(segments)
        # Sentence terminators were consumed as boundaries; all OTHER
        # characters (including every diacritic) must survive reassembly:
        # dropped chars are exactly ".", " " and "!".
        assert len(joined) == len(text) - 3
        for char in "ếệữốềủấ":
            assert char in joined
        assert "?" not in joined  # nothing mangled into placeholder garbage

    def test_deterministic(self) -> None:
        text = "Một câu dùng để thử. Hai câu nữa để kiểm tra! Ba là chốt."
        assert split_text_for_streaming(text) == split_text_for_streaming(text)
        assert split_text_for_streaming(text, 25) == split_text_for_streaming(text, 25)

    def test_invalid_max_chars_raises(self) -> None:
        with pytest.raises(ValueError):
            split_text_for_streaming("abc", max_chars=0)


class StreamingFake(FakeVieneu):
    """FakeVieneu whose infer_stream yields TAGGED chunks and records calls.

    Each call emits full(1536, 1.0) then full(2304, 2.0) so tests can verify
    chunk ordering across segment joins.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stream_texts: list[str] = []
        self.stream_kwargs: list[dict[str, Any]] = []

    def infer_stream(self, text, voice=None, **kw) -> Iterator[np.ndarray]:
        self.calls.append(("infer_stream", {"text": text, "voice": voice, "extra": dict(kw)}))
        self.stream_texts.append(text)
        self.stream_kwargs.append({"voice": voice, **kw})
        yield np.full(1536, 1.0, dtype=np.float32)
        yield np.full(2304, 2.0, dtype=np.float32)


class TestInferStreamChunked:
    """TTSEngine.infer_stream_chunked: chained per-segment SDK streams."""

    def test_single_segment_equivalent_to_infer_stream(self) -> None:
        engine = make_engine()
        text = "Câu ngắn không cần chia đoạn"
        via_chunked = list(engine.infer_stream_chunked(text, voice="Adam"))
        via_plain = list(engine.infer_stream(text, voice="Adam"))
        assert [c.shape for c in via_chunked] == [c.shape for c in via_plain]
        # Underlying SDK surface identical to the legacy path: one call,
        # full text, voice forwarded, no accidental sampling overrides.
        assert FakeVieneu.instances[0].calls[0] == (
            "infer_stream",
            {"text": text, "voice": "Adam"},
        )

    def test_multi_segment_chunks_in_segment_order(self) -> None:
        engine = make_engine(factory=lambda **kw: StreamingFake(**kw))
        text = " ".join(_sentence(f"Câu thứ {i}", 120) for i in range(8))
        expected_segments = split_text_for_streaming(text, max_chars=250)
        assert len(expected_segments) >= 2

        chunks = list(engine.infer_stream_chunked(text, max_chars=250))

        fake = FakeVieneu.instances[0]
        assert isinstance(fake, StreamingFake)
        assert fake.stream_texts == expected_segments
        # Two tagged chunks per segment: values cycle 1.0, 2.0 per segment.
        values = [float(np.unique(np.asarray(c))[0]) for c in chunks]
        n = len(expected_segments)
        assert len(chunks) == 2 * n
        assert values == ([1.0, 2.0] * n)
        assert chunks[0].shape == (1536,) and chunks[1].shape == (2304,)
        assert chunks[2].shape == (1536,)

    def test_voice_and_temperature_passthrough(self) -> None:
        engine = make_engine(factory=lambda **kw: StreamingFake(**kw))
        text = " ".join(_sentence(f"Mẫu {i}", 90) for i in range(8))
        list(engine.infer_stream_chunked(text, voice="Minh", temperature=0.35))
        fake = FakeVieneu.instances[0]
        assert fake.stream_kwargs[0]["voice"] == "Minh"
        assert fake.stream_kwargs[0]["temperature"] == 0.35

    def test_none_temperature_forwarded_like_infer_stream(self) -> None:
        engine = make_engine(factory=lambda **kw: StreamingFake(**kw))
        list(engine.infer_stream_chunked("Ngắn gọn vậy thôi."))
        fake = FakeVieneu.instances[0]
        assert fake.stream_kwargs[0]["temperature"] is None

    def test_error_wrapped_as_tts_engine_error_with_cause(self) -> None:
        class StreamBoom(FakeVieneu):
            def infer_stream(self, text, voice=None, **kw):
                yield silent(100)
                raise ValueError("codec exploded")

        engine = TTSEngine(factory=lambda **kw: StreamBoom(**kw))
        stream = engine.infer_stream_chunked("text ok")
        first = next(stream)
        assert first.shape == (100,)
        with pytest.raises(TTSEngineError, match="infer_stream failed") as excinfo:
            next(stream)
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_models_missing_classified_on_first_next(self) -> None:
        def factory(**kw: Any):
            raise FileNotFoundError("No such file or directory: 'hf-cache/snapshots/x.onnx'")

        engine = TTSEngine(factory=factory)
        with pytest.raises(ModelsMissingError):
            next(engine.infer_stream_chunked("hi"))

    def test_whitespace_only_yields_nothing_but_engine_still_loads(self) -> None:
        engine = make_engine()
        assert list(engine.infer_stream_chunked("   \n ")) == []
        assert engine.is_initialized  # lazy init ran (same seam as infer_stream)
        fake = FakeVieneu.instances[0]
        assert not any(op == "infer_stream" for op, _ in fake.calls)

    def test_max_chars_override_respected(self) -> None:
        engine = make_engine(factory=lambda **kw: StreamingFake(**kw))
        text = " ".join(_sentence(f"Trích đoạn số {i}", 80) for i in range(12))
        list(engine.infer_stream_chunked(text, max_chars=150))
        fake = FakeVieneu.instances[0]
        expected = split_text_for_streaming(text, max_chars=150)
        assert fake.stream_texts == expected
        assert all(len(seg) <= 150 for seg in fake.stream_texts)

    def test_lazy_factory_not_called_until_iteration(self) -> None:
        engine = make_engine()
        stream = engine.infer_stream_chunked("đợi đi")
        assert engine.is_initialized is False
        next(stream)
        assert engine.is_initialized is True
