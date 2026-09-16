"""Benchmark pack: corpus, schema, statistics, resources, and summary contracts."""

import builtins
import hashlib
import json
import time
from pathlib import Path

import pytest
from scripts.benchmarks import managed_cuda, resources
from scripts.benchmarks.corpus import CORPUS, get_corpus_entry
from scripts.benchmarks.resources import (
    CudaMemorySample,
    ResourceSampler,
    _parse_proc_status,
    _parse_ps_rss,
)
from scripts.benchmarks.schema import (
    BenchmarkRecord,
    BenchmarkScenario,
    environment_manifest,
    write_jsonl,
)
from scripts.benchmarks.statistics import summarize
from scripts.benchmarks.summarize import summarize_records

from vienetts_app.core.cuda_runtime_manifest import CudaRuntimeManifest

EXPECTED_IDS = {
    "vi_20",
    "vi_50",
    "vi_256",
    "vi_512",
    "vi_2000",
    "vi_5000",
    "en_short",
    "code_switch",
    "numbers",
    "emotion",
    "multiline",
    "punctuation_free",
}


class TestCorpus:
    def test_corpus_contains_expected_ids_and_hash_stable(self) -> None:
        assert set(CORPUS) == EXPECTED_IDS
        for scenario_id, entry in CORPUS.items():
            assert entry.scenario_id == scenario_id
            assert entry.text.strip()
            assert isinstance(entry.text.encode("utf-8"), bytes)
            assert entry.sha256 == hashlib.sha256(entry.text.encode("utf-8")).hexdigest()
            assert entry.identity()["char_count"] == len(entry.text)

    def test_get_corpus_entry_rejects_unknown_id(self) -> None:
        try:
            get_corpus_entry("missing")
        except KeyError as exc:
            assert "missing" in str(exc)
        else:
            raise AssertionError("unknown corpus ID should raise KeyError")


class TestSchema:
    def test_environment_excludes_identity_fields(self) -> None:
        manifest = environment_manifest()
        payload = manifest.to_dict()
        serialized = json.dumps(payload)
        forbidden = {"hostname", "serial", "hardware_uuid", "username", "home"}

        assert not forbidden.intersection(payload)
        assert str(Path.home()) not in serialized

    def test_record_contains_corpus_identity_not_text(self) -> None:
        entry = get_corpus_entry("vi_50")
        scenario = BenchmarkScenario.from_entry(
            entry,
            backend="onnx",
            precision="int8",
            mode="stream",
        )

        payload = scenario.to_dict()

        assert payload["scenario_id"] == "vi_50"
        assert payload["text_sha256"] == entry.sha256
        assert payload["char_count"] == len(entry.text)
        assert entry.text not in json.dumps(payload, ensure_ascii=False)

    def test_scenario_preserves_resolved_backend(self) -> None:
        entry = get_corpus_entry("vi_20")
        scenario = BenchmarkScenario.from_entry(
            entry,
            backend="onnx",
            resolved_backend="onnx",
            precision="int8",
            mode="infer",
        )

        assert scenario.to_dict()["resolved_backend"] == "onnx"

    def test_write_jsonl_emits_one_valid_json_object_per_line(self, tmp_path: Path) -> None:
        entry = get_corpus_entry("vi_20")
        scenario = BenchmarkScenario.from_entry(
            entry,
            backend="onnx",
            precision="int8",
            mode="infer",
        )
        record = BenchmarkRecord(
            environment=environment_manifest(hardware_class="fake-ci"),
            scenario=scenario,
            trace={"outcome": "completed"},
            resources={"sample_count": 1},
            elapsed_ns=10,
            audio_duration_ms=2,
        )
        output = tmp_path / "records.jsonl"

        write_jsonl([record, record], output)

        lines = output.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert all(json.loads(line)["schema_version"] == 1 for line in lines)

    def test_record_rejects_negative_durations_or_bytes(self) -> None:
        entry = get_corpus_entry("vi_20")
        scenario = BenchmarkScenario.from_entry(
            entry,
            backend="onnx",
            precision="int8",
            mode="infer",
        )

        with pytest.raises(ValueError, match="non-negative"):
            BenchmarkRecord(
                environment=environment_manifest(),
                scenario=scenario,
                trace={"outcome": "completed"},
                resources={"max_rss_bytes": -1},
                elapsed_ns=10,
                audio_duration_ms=2,
            )


class TestStatistics:
    def test_distribution_uses_nearest_rank_percentiles(self) -> None:
        result = summarize([1.0, 2.0, 3.0, 4.0, 100.0])

        assert result.count == 5
        assert result.minimum == 1.0
        assert result.median == 3.0
        assert result.p90 == 100.0
        assert result.p95 == 100.0
        assert result.maximum == 100.0
        assert result.mad == 1.0

    def test_empty_input_and_singleton(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            summarize([])

        result = summarize([4.5])
        assert result.count == 1
        assert result.minimum == 4.5
        assert result.median == 4.5
        assert result.p90 == 4.5
        assert result.p95 == 4.5
        assert result.maximum == 4.5
        assert result.mad == 0.0


class TestResources:
    def test_parse_linux_proc_status_and_ps_rss(self) -> None:
        text = "Name:\tpython\nVmRSS:\t  12345 kB\nVmHWM:\t  23456 kB\n"

        current, peak = _parse_proc_status(text)

        assert current == 12_345 * 1024
        assert peak == 23_456 * 1024

        current, peak = _parse_proc_status("VmRSS:\t64 kB\n")
        assert current == 64 * 1024
        assert peak == current

        assert _parse_ps_rss(" 2048\n") == 2 * 1024 * 1024

    def test_sampler_preserves_last_and_maximum_samples(self, monkeypatch) -> None:
        rss_values = iter([100, 250, 175])
        peak_values = iter([120, 270, 200])
        cpu_values = iter([10, 25, 55])

        monkeypatch.setattr(resources, "current_rss_bytes", lambda pid=None: next(rss_values))
        monkeypatch.setattr(resources, "peak_rss_bytes", lambda: next(peak_values))
        monkeypatch.setattr(time, "process_time_ns", lambda: next(cpu_values))

        sampler = ResourceSampler(interval_seconds=0.001)
        sampler.start()
        sampler.start()  # idempotent: a live sampler is not restarted
        time.sleep(0.01)
        sampler.stop()
        sampler.stop()  # idempotent: stopping twice is a no-op

        result = sampler.result()
        assert result.sample_count >= 1
        assert result.current_rss_bytes == result.samples[-1].current_rss_bytes
        assert result.max_current_rss_bytes == max(
            sample.current_rss_bytes for sample in result.samples
        )
        assert result.peak_rss_bytes == max(sample.peak_rss_bytes for sample in result.samples)
        assert result.process_cpu_delta_ns >= 0

    def test_sampler_reports_cuda_values_from_injected_probe(self) -> None:
        expected = CudaMemorySample(
            allocated_bytes=1,
            reserved_bytes=2,
            maximum_allocated_bytes=3,
            maximum_reserved_bytes=4,
        )
        sampler = ResourceSampler(
            interval_seconds=0.001,
            sample_cuda=True,
            cuda_probe=lambda: expected,
        )

        sampler.start()
        time.sleep(0.003)
        sampler.stop()

        result = sampler.result()
        assert result.samples
        assert result.samples[-1].cuda == expected

    def test_sampler_without_cuda_does_not_import_torch(self, monkeypatch) -> None:
        original_import = builtins.__import__

        def reject_torch(name, *args, **kwargs):
            if name == "torch" or name.startswith("torch."):
                raise AssertionError("torch must not be imported")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_torch)
        sampler = ResourceSampler(interval_seconds=0.001, sample_cuda=False)

        sampler.start()
        time.sleep(0.003)
        sampler.stop()

        assert sampler.result().sample_count >= 1

    def test_cuda_memory_sample_rejects_negative_values(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            CudaMemorySample(
                allocated_bytes=-1,
                reserved_bytes=0,
                maximum_allocated_bytes=0,
                maximum_reserved_bytes=0,
            )


class TestSummary:
    def _record(self, *, run_kind: str, elapsed_ms: float) -> dict[str, object]:
        events = [
            {"name": "engine_call_started", "offset_ns": 10_000_000},
            {"name": "engine_first_chunk", "offset_ns": 30_000_000},
            {"name": "engine_initialize_started", "offset_ns": 1_000_000},
            {"name": "engine_initialize_completed", "offset_ns": 9_000_000},
        ]
        trace: dict[str, object] = {"events": events}
        if run_kind == "direct_engine":
            trace["run_kind"] = run_kind
        else:
            trace["events"] = [
                {"name": "submitted", "offset_ns": 10_000_000},
                {"name": "worker_first_chunk", "offset_ns": 30_000_000},
            ]
        return {
            "scenario": {
                "scenario_id": "vi_50",
                "backend": "onnx",
                "precision": "int8",
                "sink_kind": "none" if run_kind == "direct_engine" else "fake",
            },
            "trace": trace,
            "elapsed_ms": elapsed_ms,
        }

    def test_summary_groups_direct_and_pipeline_metrics(self) -> None:
        payload = summarize_records(
            [
                self._record(run_kind="direct_engine", elapsed_ms=20.0),
                self._record(run_kind="pipeline", elapsed_ms=40.0),
            ]
        )

        groups = payload["groups"]
        assert isinstance(groups, list)
        assert {group["key"]["path"] for group in groups} == {"direct", "pipeline"}
        direct = next(group for group in groups if group["key"]["path"] == "direct")
        assert direct["distributions"]["ttfc_ms"]["median"] == 20.0
        assert direct["distributions"]["model_initialization_ms"]["median"] == 8.0


def _mini_manifest() -> CudaRuntimeManifest:
    return CudaRuntimeManifest("test-v1", "linux-x64", "cp313", ())


def _fabricate_ready_install(root: Path, manifest: CudaRuntimeManifest) -> Path:
    """A metadata-exact install the manager's inspect() accepts (no wheels)."""
    active = root / manifest.format_version
    (active / "site-packages").mkdir(parents=True)
    metadata = {
        "format": manifest.format_version,
        "platform": manifest.platform_key,
        "python_tag": manifest.python_tag,
        "wheels": {wheel.filename: wheel.sha256 for wheel in manifest.wheels},
    }
    (active / "install.json").write_text(json.dumps(metadata), encoding="utf-8")
    return active


class TestManagedCudaHarness:
    def test_default_root_uses_app_data_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(managed_cuda, "default_data_dir", lambda: tmp_path)

        assert managed_cuda.default_cuda_runtime_root() == tmp_path / "runtime" / "cuda"

    def test_returns_none_when_platform_has_no_manifest(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(managed_cuda, "manifest_for_platform", lambda _key: None)

        assert managed_cuda.resolve_managed_cuda_location(tmp_path) is None

    def test_returns_none_when_runtime_not_installed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(managed_cuda, "manifest_for_platform", lambda _key: _mini_manifest())

        assert managed_cuda.resolve_managed_cuda_location(tmp_path) is None

    def test_resolves_ready_runtime_root(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _mini_manifest()
        monkeypatch.setattr(managed_cuda, "manifest_for_platform", lambda _key: manifest)
        active = _fabricate_ready_install(tmp_path, manifest)

        location = managed_cuda.resolve_managed_cuda_location(tmp_path)

        assert location is not None
        assert location.root == active
        assert location.site_packages == active / "site-packages"
        assert location.format_version == manifest.format_version
        assert location.python_tag == manifest.python_tag

    def test_accepts_versioned_install_directory(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _mini_manifest()
        monkeypatch.setattr(managed_cuda, "manifest_for_platform", lambda _key: manifest)
        active = _fabricate_ready_install(tmp_path, manifest)

        location = managed_cuda.resolve_managed_cuda_location(active)

        assert location is not None
        assert location.root == active

    def test_rejects_mismatched_install_metadata(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _mini_manifest()
        monkeypatch.setattr(managed_cuda, "manifest_for_platform", lambda _key: manifest)
        active = tmp_path / manifest.format_version
        (active / "site-packages").mkdir(parents=True)
        (active / "install.json").write_text('{"format": "other"}', encoding="utf-8")

        assert managed_cuda.resolve_managed_cuda_location(tmp_path) is None
        assert managed_cuda.resolve_managed_cuda_location(active) is None

    def test_torch_backend_requires_ready_runtime(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(managed_cuda, "manifest_for_platform", lambda _key: _mini_manifest())

        with pytest.raises(SystemExit, match="managed CUDA runtime"):
            managed_cuda.cuda_runtime_for_backend("torch", tmp_path)

    def test_torch_backend_returns_ready_location(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _mini_manifest()
        monkeypatch.setattr(managed_cuda, "manifest_for_platform", lambda _key: manifest)
        active = _fabricate_ready_install(tmp_path, manifest)

        location = managed_cuda.cuda_runtime_for_backend("torch", tmp_path)

        assert location is not None
        assert location.root == active

    def test_onnx_backend_needs_no_runtime(self) -> None:
        assert managed_cuda.cuda_runtime_for_backend("onnx", None) is None

    def test_run_engine_wires_resolved_runtime_into_torch_engine(self, monkeypatch) -> None:
        from scripts.benchmarks import run_engine

        sentinel = object()
        captured: dict[str, object] = {}

        class FakeEngine:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(run_engine, "TTSEngine", FakeEngine)
        monkeypatch.setattr(run_engine, "cuda_runtime_for_backend", lambda backend, root: sentinel)
        args = run_engine._parser().parse_args(["--engine", "real", "--backend", "torch"])

        engine = run_engine._make_engine(args)

        assert isinstance(engine, FakeEngine)
        assert captured["cuda_runtime"] is sentinel

    def test_run_engine_fake_engine_needs_no_runtime(self) -> None:
        from scripts.benchmarks import run_engine
        from scripts.benchmarks.fakes import DeterministicEngine

        args = run_engine._parser().parse_args(["--engine", "fake", "--backend", "torch"])

        assert isinstance(run_engine._make_engine(args), DeterministicEngine)

    def test_run_matrix_forwards_cuda_runtime(self, tmp_path: Path) -> None:
        from scripts.benchmarks import run_matrix

        args = run_matrix._parser().parse_args(["--cuda-runtime", str(tmp_path)])
        command = run_matrix._child_command(args, "vi_50", tmp_path / "out.jsonl")

        assert "--cuda-runtime" in command
        assert str(tmp_path) in command

    def test_run_matrix_omits_cuda_runtime_by_default(self, tmp_path: Path) -> None:
        from scripts.benchmarks import run_matrix

        args = run_matrix._parser().parse_args([])
        command = run_matrix._child_command(args, "vi_50", tmp_path / "out.jsonl")

        assert "--cuda-runtime" not in command
