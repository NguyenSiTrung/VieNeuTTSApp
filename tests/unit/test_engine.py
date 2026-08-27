"""TTSEngine: lazy single-instance ownership, SDK wrappers, error propagation."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core.engine import TTSEngine, TTSEngineError, preset_voices, saved_voice_names


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
    return TTSEngine(factory=lambda **kw: FakeVieneu(**kw), **kwargs)


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

    def test_init_kwargs_forwarded(self) -> None:
        engine = make_engine(backend="onnx", precision="int8")
        engine.infer("hi")
        assert FakeVieneu.instances[0].init_kwargs == {"backend": "onnx", "precision": "int8"}

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
