"""Qwen GGUF native probe: CLI validation, verdict schema, matrix manifest.

`scripts/spike/qwen_gguf_probe.py` produces ONE machine-readable verdict per
(profile, quantization, cell) run against the pinned qwentts.cpp shared
library (track `qwen_gguf_engine_20260923`, Task 1.1). Every native call goes
through an injectable ABI object, so these tests never load a real library or
model weights. `packaging/qwen-gguf-runtime-requirements.json` pins the six
release cells and the four model variants the probe evidences.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from scripts.spike import qwen_gguf_probe as probe

REQUIREMENTS_PATH = Path(__file__).parents[2] / "packaging" / "qwen-gguf-runtime-requirements.json"

# The complete release matrix from the spec: three OS families, each with a
# CPU cell plus the platform's GPU backend. Keys must not silently change.
REQUIRED_CELLS = {
    "windows-x64-cpu": "cpu",
    "windows-x64-cuda": "cuda",
    "linux-x64-cpu": "cpu",
    "linux-x64-cuda": "cuda",
    "macos-arm64-cpu": "cpu",
    "macos-arm64-metal": "metal",
}


class FakeAbi:
    """In-process qwentts.cpp double implementing the probe's ABI surface.

    Mirrors the pinned `src/qwen.h` contract: `synthesize` runs streaming or
    buffered mode, honours the cancel callback at decode-step granularity, and
    returns upstream status codes. Behavior switches model every failure the
    probe must classify.
    """

    def __init__(
        self,
        *,
        version: str = "0cbde9b (2026-09-22)",
        abi_ok: bool = True,
        missing_symbol: str = "",
        init_status: int = 0,
        init_error: str = "",
        model_type: str = "custom_voice",
        speakers: tuple[str, ...] = ("ryan", "vivian", "uncle_fu"),
        languages: tuple[str, ...] = ("english", "chinese"),
        num_codebooks: int = 16,
        audio_seconds: float = 0.5,
        chunk_samples: int = 4800,
        non_finite: bool = False,
        silent: bool = False,
        empty_audio: bool = False,
        synth_status: int = 0,
        synth_error: str = "",
        cancel_supported: bool = True,
        free_raises: bool = False,
        backend_name: str = "CPU",
    ) -> None:
        self._version = version
        self.abi_ok = abi_ok
        self.missing_symbol = missing_symbol
        self.init_status = init_status
        self.init_error = init_error
        self.model_type = model_type
        self.speakers = speakers
        self.languages = languages
        self.num_codebooks = num_codebooks
        self.audio_seconds = audio_seconds
        self.chunk_samples = chunk_samples
        self.non_finite = non_finite
        self.silent = silent
        self.empty_audio = empty_audio
        self.synth_status = synth_status
        self.synth_error = synth_error
        self.cancel_supported = cancel_supported
        self.free_raises = free_raises
        self.backend_name = backend_name
        self.last_error_text = ""
        self.init_calls: list[dict[str, object]] = []
        self.synth_calls: list[dict[str, object]] = []
        self.freed: list[int] = []
        self.log_lines: list[str] = []
        self._next_ctx = 1

    # ── probe-facing ABI surface (same names as the ctypes binding) ──

    def missing_symbols(self) -> list[str]:
        return [self.missing_symbol] if self.missing_symbol else []

    def version(self) -> str:
        return self._version

    def last_error(self) -> str:
        return self.last_error_text

    def set_log_callback(self, _cb) -> None:
        return None

    def init(self, **kwargs) -> int:
        self.init_calls.append(dict(kwargs))
        self.log_lines.append(f"[Load] TTS backend: {self.backend_name} (CPU threads: 9)")
        if self.init_status != 0:
            self.last_error_text = self.init_error
            raise probe.NativeInitError(self.init_status, self.init_error)
        ctx = self._next_ctx
        self._next_ctx += 1
        return ctx

    def model_type_of(self, _ctx) -> str:
        return self.model_type

    def speakers_of(self, _ctx) -> list[str]:
        return list(self.speakers)

    def languages_of(self, _ctx) -> list[str]:
        return list(self.languages)

    def num_codebooks_of(self, _ctx) -> int:
        return self.num_codebooks

    def extract_voice_ref(self, _ctx, pcm: np.ndarray) -> dict:
        if self.model_type != "base":
            self.last_error_text = "voice references are only valid for base models"
            raise probe.NativeCallError(-2, self.last_error_text)
        return {
            "spkEmbDim": 1024,
            "refT": max(1, pcm.size // 480),
            "numCodebooks": self.num_codebooks,
        }

    def synthesize(self, ctx, *, on_chunk=None, cancel=None, **_params) -> int:
        self.synth_calls.append(dict(_params))
        if self.synth_status != 0:
            self.last_error_text = self.synth_error
            return self.synth_status
        if self.empty_audio:
            return 0
        total = int(24000 * self.audio_seconds)
        emitted = 0
        while emitted < total:
            if cancel is not None and cancel():
                return -5  # QT_STATUS_CANCELLED
            n = min(self.chunk_samples, total - emitted)
            chunk = np.full(n, 0.0 if self.silent else 0.1, dtype=np.float32)
            if self.non_finite:
                chunk[0] = np.nan
            emitted += n
            if on_chunk is not None and not on_chunk(chunk):
                return -5
        return 0

    def free(self, ctx) -> None:
        if self.free_raises:
            raise RuntimeError("native teardown fault")
        self.freed.append(ctx)


def make_request(tmp_path: Path, **kwargs) -> probe.ProbeRequest:
    talker = tmp_path / "talker.gguf"
    tokenizer = tmp_path / "tokenizer.gguf"
    talker.write_bytes(b"GGUF-talker")
    tokenizer.write_bytes(b"GGUF-tokenizer")
    library = tmp_path / "libqwen.so"
    library.write_bytes(b"ELF")
    defaults: dict[str, object] = {
        "profile": "customvoice",
        "quantization": "Q8_0",
        "talker": str(talker),
        "tokenizer": str(tokenizer),
        "library": str(library),
        "speaker": "ryan",
        "hash_models": False,
    }
    defaults.update(kwargs)
    return probe.ProbeRequest(**defaults)


def run_with(abi: FakeAbi, tmp_path: Path, **kwargs) -> dict:
    request = make_request(tmp_path, **kwargs)
    return probe.run_probe(
        request,
        abi_factory=lambda _path: abi,
        # 2 GiB in the unit ``_default_rss_fn`` reports on THIS platform
        # (ru_maxrss is bytes on macOS, KB elsewhere).
        rss_fn=lambda: 2 * 1024**3 if sys.platform == "darwin" else 2 * 1024**2,
        environ={},
    )


class TestCliValidation:
    def test_inputs_are_validated_before_any_native_load(self, tmp_path) -> None:
        talker = tmp_path / "t.gguf"
        talker.write_bytes(b"x")
        tokenizer = tmp_path / "c.gguf"
        tokenizer.write_bytes(b"x")
        library = tmp_path / "libqwen.so"
        library.write_bytes(b"ELF")
        base = [
            "--profile",
            "customvoice",
            "--quantization",
            "Q8_0",
            "--talker",
            str(talker),
            "--tokenizer",
            str(tokenizer),
            "--library",
            str(library),
        ]
        with pytest.raises(probe.ProbeUsageError, match="library"):
            probe.validate_request(probe.parse_args(base[:-1] + [str(tmp_path / "no.so")]))
        with pytest.raises(probe.ProbeUsageError, match="speaker"):
            probe.validate_request(probe.parse_args(base))
        with pytest.raises(probe.ProbeUsageError, match="ref-audio"):
            probe.validate_request(
                probe.parse_args([a if a != "customvoice" else "base" for a in base])
            )
        with pytest.raises(probe.ProbeUsageError, match="unknown profile"):
            probe.validate_request(
                probe.parse_args([a if a != "customvoice" else "voice_design" for a in base])
            )
        with pytest.raises(probe.ProbeUsageError, match="quantization"):
            probe.validate_request(probe.parse_args([a if a != "Q8_0" else "F32" for a in base]))
        with pytest.raises(probe.ProbeUsageError, match="device"):
            probe.validate_request(probe.parse_args(base + ["--device", "vulkan"]))

    def test_valid_request_carries_track_defaults(self, tmp_path) -> None:
        request = make_request(tmp_path, profile="base", ref_audio="r.wav", ref_text="t")
        assert request.engine == "qwentts_cpp"
        assert request.device == "auto"
        assert request.language == "auto"
        assert request.cancel_after_ms == 250


class TestVerdicts:
    def test_success_verdict_carries_full_evidence(self, tmp_path) -> None:
        payload = run_with(FakeAbi(), tmp_path)
        assert payload["verdict"] == "pass"
        assert payload["schemaVersion"] == probe.SCHEMA_VERSION
        assert payload["kind"] == "qwen-gguf-probe"
        assert payload["engine"] == "qwentts_cpp"
        assert payload["runtime"]["qtVersion"] == "0cbde9b (2026-09-22)"
        assert payload["runtime"]["abiVersion"] == probe.QT_ABI_VERSION
        assert payload["device"]["resolvedBackend"] == "CPU"
        caps = payload["capabilities"]
        assert payload["model"]["modelType"] == "custom_voice"
        assert caps["speakers"] == ["ryan", "vivian", "uncle_fu"]
        assert caps["languages"] == ["english", "chinese"]
        assert caps["numCodebooks"] == 16
        metrics = payload["metrics"]
        assert metrics["audioSeconds"] == pytest.approx(0.5)
        assert metrics["ttfaMs"] is not None
        # rtf recomputes from the recorded totals; a sub-ms fake may round to 0.
        assert metrics["rtf"] == pytest.approx(
            metrics["totalMs"] / 1000 / metrics["audioSeconds"], abs=0.002
        )
        assert metrics["peakRssMb"] == pytest.approx(2048.0)
        assert payload["streaming"]["chunkCount"] >= 2
        assert payload["shutdown"]["clean"] is True
        assert payload["errors"] == []
        json.dumps(payload, allow_nan=False)

    def test_usage_error_is_one_json_object_with_nonzero_exit(self, tmp_path, capsys) -> None:
        exit_code = probe.main(["--profile", "base"])
        payload = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 2
        assert payload["verdict"] == "usage_error"
        assert payload["errors"]

    def test_missing_library_and_failed_init_are_missing_backend(self, tmp_path) -> None:
        payload = run_with(FakeAbi(missing_symbol="qt_init"), tmp_path)
        assert payload["verdict"] == "abi_mismatch"

        init_fail = run_with(FakeAbi(init_status=-1, init_error="no backend available"), tmp_path)
        assert init_fail["verdict"] == "missing_backend"
        assert "no backend" in init_fail["errors"][0]

        request = make_request(tmp_path, library=str(tmp_path / "absent.so"))
        payload = probe.run_probe(
            request, abi_factory=probe._ctypes_abi_factory, rss_fn=lambda: 0, environ={}
        )
        assert payload["verdict"] == "missing_backend"

    def test_abi_version_rejection_is_classified(self, tmp_path) -> None:
        abi = FakeAbi(
            init_status=-1, init_error="params->abi_version 4 outside the supported range"
        )
        abi.init_error = "abi_version"
        payload = run_with(abi, tmp_path)
        assert payload["verdict"] == "abi_mismatch"

    def test_invalid_speaker_and_mode_mismatch_are_distinct(self, tmp_path) -> None:
        payload = run_with(FakeAbi(speakers=("ryan",)), tmp_path, speaker="nobody")
        assert payload["verdict"] == "invalid_speaker"

        wrong_type = run_with(FakeAbi(model_type="base"), tmp_path)
        assert wrong_type["verdict"] == "mode_mismatch"
        assert "model_type" in wrong_type["errors"][0]

    def test_cancelled_primary_synthesis_is_a_cancelled_verdict(self, tmp_path) -> None:
        payload = run_with(FakeAbi(synth_status=-5, synth_error="cancelled"), tmp_path)
        assert payload["verdict"] == "cancelled"

    def test_empty_nonfinite_and_silent_audio_are_bad_audio(self, tmp_path) -> None:
        for abi in (
            FakeAbi(empty_audio=True),
            FakeAbi(non_finite=True),
            FakeAbi(silent=True),
        ):
            payload = run_with(abi, tmp_path)
            assert payload["verdict"] == "bad_audio", payload["errors"]


class TestCancellationEvidence:
    def test_native_cancel_callback_reports_interruptible_with_latency(self, tmp_path) -> None:
        # cancel_after_ms=0: the cancel callback trips on the first decode-step
        # poll, which is exactly what the real library does every ~83 ms.
        payload = run_with(FakeAbi(), tmp_path, check_cancel=True, cancel_after_ms=0)
        cancellation = payload["cancellation"]
        assert payload["verdict"] == "pass"
        assert cancellation["requested"] is True
        assert cancellation["interruptible"] is True
        assert cancellation["terminal"] == "cancelled"
        assert cancellation["latencyMs"] is not None

    def test_uninterruptible_synthesis_is_recorded_not_hidden(self, tmp_path) -> None:
        abi = FakeAbi()
        original = abi.synthesize

        def ignore_cancel(ctx, *, cancel=None, **kwargs):
            return original(ctx, cancel=None, **kwargs)

        abi.synthesize = ignore_cancel
        payload = run_with(abi, tmp_path, check_cancel=True, cancel_after_ms=0)
        assert payload["cancellation"]["interruptible"] is False
        assert payload["cancellation"]["terminal"] == "completed"


class TestBaseProfileFlow:
    def test_base_extracts_reference_latents_before_synthesis(self, tmp_path) -> None:
        pcm = np.full(24000 * 3, 0.05, dtype=np.float32)
        wav = tmp_path / "ref.wav"
        probe._write_wav_24k(wav, pcm)
        abi = FakeAbi(model_type="base")
        payload = run_with(
            abi, tmp_path, profile="base", speaker="", ref_audio=str(wav), ref_text="transcript"
        )
        assert payload["verdict"] == "pass"
        assert payload["refExtraction"]["spkEmbDim"] == 1024
        call = abi.synth_calls[0]
        assert call["speaker"] is None
        assert call["ref_text"] == "transcript"
        assert call["ref_audio_24k"] is not None


@pytest.fixture(scope="module")
def requirements() -> dict:
    return json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))


class TestRequirementsManifest:
    def test_six_cells_four_variants_twenty_four_combinations(self, requirements) -> None:
        assert len(requirements["cells"]) == 6
        assert {c["key"] for c in requirements["cells"]} == set(REQUIRED_CELLS)
        assert {v["quantization"] for v in requirements["variants"]} == {"Q8_0", "Q4_K_M"}
        assert len(requirements["variants"]) == 4
        assert {v["profile"] for v in requirements["variants"]} == {"base", "customvoice"}
        assert (
            len(requirements["cells"]) * len(requirements["variants"])
            == requirements["combinations"]
            == 24
        )

    def test_every_variant_pairs_matching_tokenizer_and_pins_digests(self, requirements) -> None:
        revision = requirements["upstream"]["models"]["revision"]
        assert len(revision) == 40
        for variant in requirements["variants"]:
            for part in ("talker", "tokenizer"):
                pinned = variant[part]
                assert pinned["file"].endswith(f"{variant['quantization']}.gguf")
                assert len(pinned["sha256"]) == 64
                assert pinned["size"] > 0
            assert variant["expectedModelType"] == (
                "base" if variant["profile"] == "base" else "custom_voice"
            )
        # Same quantization => same tokenizer artifact shared across profiles.
        by_quant = {}
        for variant in requirements["variants"]:
            by_quant.setdefault(variant["quantization"], set()).add(variant["tokenizer"]["sha256"])
        assert all(len(digests) == 1 for digests in by_quant.values())

    def test_runtime_pin_records_commit_header_and_shared_library(self, requirements) -> None:
        runtime = requirements["upstream"]["runtime"]
        assert len(runtime["commit"]) == 40
        assert len(runtime["ggmlSubmoduleCommit"]) == 40
        assert runtime["abiVersion"] == probe.QT_ABI_VERSION
        assert runtime["abiMinVersion"] <= probe.QT_ABI_VERSION
        assert runtime["sharedLibrary"]["cmakeFlag"] == "QWEN_SHARED=ON"
        for key in ("linux", "windows", "macos"):
            assert runtime["sharedLibrary"]["names"][key]

    def test_pending_cells_declare_probe_commands(self, requirements) -> None:
        pending = []
        expected_variants = {
            f"{v['profile']}-{v['quantization']}" for v in requirements["variants"]
        }
        for cell in requirements["cells"]:
            evidence = cell["evidence"]
            assert evidence["status"] in ("verified", "pending")
            assert cell["device"] == REQUIRED_CELLS[cell["key"]]
            assert cell["ggmlBackend"]
            if evidence["status"] == "pending":
                pending.append(cell["key"])
                continue
            seen = set()
            for rel in evidence["evidencePaths"]:
                path = REQUIREMENTS_PATH.parents[1] / rel
                assert path.is_file(), f"{cell['key']} claims verified but {path} is missing"
                payload = json.loads(path.read_text(encoding="utf-8"))
                assert payload["schemaVersion"] == probe.SCHEMA_VERSION
                assert payload["verdict"] == "pass"
                seen.add(f"{payload['profile']}-{payload['quantization']}")
            assert seen == expected_variants, (
                f"{cell['key']} is verified but evidence covers {sorted(seen)}"
            )
        assert requirements["pendingCells"] == pending
