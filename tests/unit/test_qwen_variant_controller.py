"""Task 5.1 (track qwen_gguf_engine_20260923): variant-aware Qwen controller state.

The settings carry three independent selections — ``qwen_model_format``
(official/gguf), ``qwen_gguf_quantization`` (Q8_0/Q4_K_M) and ONE device
preference per format (``qwen_device`` in PyTorch vocabulary, ``qwen_gguf_device``
in ggml vocabulary). This suite pins the controller surface that resolves the
whole selected variant: the QML-facing selection properties, the per-format
device pickers, the inspection/install paths that must address the exact
variant, and the lifecycle rules — a variant switch is refused while work is
busy/queued/cancelling, tears the engine down only while idle, and drops every
readiness/install result stamped with an older generation.

The harness lives in :mod:`tests.unit.test_controller` — the GGUF managers and
engines are fakes there, so nothing in this file touches the network, the Hub,
or a native library.
"""

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from tests.unit.test_controller import (  # noqa: E402
    ProfileHarness,
    make_artifact,
    qwen_gguf_model_location,
    qwen_gguf_runtime_location,
    qwen_model_location,
    qwen_runtime_location,
    write_settings_file,
)

from vienetts_app.core.detector import HardwareInfo  # noqa: E402
from vienetts_app.core.engine_profiles import QWEN_CUSTOM  # noqa: E402
from vienetts_app.core.qwen_gguf_models import QwenGgufModelStatus  # noqa: E402
from vienetts_app.core.qwen_gguf_runtime import QwenGgufRuntimeStatus  # noqa: E402
from vienetts_app.core.qwen_model_manager import QwenModelStatus  # noqa: E402
from vienetts_app.core.qwen_runtime import QwenRuntimeStatus  # noqa: E402

NVIDIA = HardwareInfo(kind="nvidia", torch_installed=True, cuda_version="12.4")

_PROFILE_KEY = {"qwen_base_0_6b": "base", "qwen_custom_0_6b": "customvoice"}


def _both_ready(tmp_path: Path, **kwargs: Any) -> ProfileHarness:
    """A harness where BOTH formats' installs are verified ready."""
    kwargs.setdefault(
        "model_status",
        QwenModelStatus(state="ready", location=qwen_model_location(tmp_path)),
    )
    kwargs.setdefault(
        "runtime_status",
        QwenRuntimeStatus(state="ready", location=qwen_runtime_location(tmp_path)),
    )
    kwargs.setdefault(
        "gguf_runtime_status",
        QwenGgufRuntimeStatus(state="ready", location=qwen_gguf_runtime_location(tmp_path)),
    )
    kwargs.setdefault(
        "gguf_model_status",
        QwenGgufModelStatus(state="ready", location=qwen_gguf_model_location(tmp_path)),
    )
    kwargs.setdefault("hardware", NVIDIA)
    return ProfileHarness(tmp_path, **kwargs)


def _gguf_settings(tmp_path: Path, **extra: Any) -> None:
    write_settings_file(
        tmp_path,
        qwen_model_format="gguf",
        qwen_gguf_quantization="Q8_0",
        qwen_gguf_device="cpu",
        **extra,
    )


def _gguf_model_manager(harness: ProfileHarness, variant_key: str) -> Any:
    """The fake GGUF model manager for one variant key (``pk-quant``)."""
    for manager in harness.gguf_model_managers:
        variant = manager.variant
        key = f"{_PROFILE_KEY.get(variant.profile, variant.profile)}-{variant.quantization}"
        if key == variant_key:
            return manager
    raise AssertionError(f"no GGUF model manager was built for {variant_key}")


class TestVariantSelectionSurface:
    """The QML-facing selection: format, quantization, engine, options."""

    def test_defaults_expose_the_official_variant(self, qcoreapp, tmp_path: Path) -> None:
        controller = ProfileHarness(tmp_path).controller
        assert controller.qwenModelFormat == "official"
        assert controller.qwenGgufQuantization == "Q8_0"  # remembered inactive pref
        assert controller.qwenEngineLabel == "PyTorch"

    def test_variant_options_list_every_installable_choice(self, qcoreapp, tmp_path: Path) -> None:
        controller = ProfileHarness(tmp_path).controller
        options = controller.qwenVariantOptions
        assert [(o["format"], o["quantization"]) for o in options] == [
            ("official", ""),
            ("gguf", "Q8_0"),
            ("gguf", "Q4_K_M"),
        ]
        by_id = {o["id"]: o for o in options}
        assert by_id["official"]["engine"] == "pytorch"
        assert by_id["official"]["engineLabel"] == "PyTorch"
        assert by_id["official"]["active"] is True
        assert by_id["gguf-Q8_0"]["engine"] == "qwentts_cpp"
        assert by_id["gguf-Q8_0"]["engineLabel"] == "qwentts.cpp"
        assert by_id["gguf-Q8_0"]["active"] is False

    def test_set_variant_switches_and_persists(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness(tmp_path)
        controller = harness.controller
        assert controller.setQwenVariant("gguf", "Q4_K_M") is True
        assert controller.qwenModelFormat == "gguf"
        assert controller.qwenGgufQuantization == "Q4_K_M"
        assert controller.qwenEngineLabel == "qwentts.cpp"
        saved = harness.read_settings()
        assert saved["qwen_model_format"] == "gguf"
        assert saved["qwen_gguf_quantization"] == "Q4_K_M"
        by_id = {o["id"]: o for o in controller.qwenVariantOptions}
        assert by_id["gguf-Q4_K_M"]["active"] is True
        assert by_id["official"]["active"] is False

    def test_set_variant_defaults_a_blank_quantization(self, qcoreapp, tmp_path: Path) -> None:
        controller = ProfileHarness(tmp_path).controller
        assert controller.setQwenVariant("gguf", "") is True
        assert controller.qwenGgufQuantization == "Q8_0"

    def test_set_variant_rejects_an_unknown_combination(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness(tmp_path)
        controller = harness.controller
        for fmt, quant in (
            ("bogus", ""),
            ("official", "Q8_0"),  # official weights carry no quantization
            ("gguf", "Q2_K"),
        ):
            assert controller.setQwenVariant(fmt, quant) is False
            assert controller.qwenModelFormat == "official"
            assert controller.errorText != ""
            controller._set_error("")
        # The refused writes never reached the persisted settings.
        assert harness.read_settings().get("qwen_model_format", "official") == "official"

    def test_set_variant_same_selection_is_a_noop(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness(tmp_path)
        controller = harness.controller
        generation = controller._qwen_generation
        assert controller.setQwenVariant("official", "") is True
        assert controller._qwen_generation == generation  # nothing was re-resolved


class TestDevicePreferences:
    """One device preference per format — neither may overwrite the other."""

    def test_device_preferences_survive_format_switches(self, qcoreapp, tmp_path: Path) -> None:
        harness = _both_ready(tmp_path)
        controller = harness.controller
        assert controller.setQwenDevice("cuda") is True  # official pref
        assert controller.qwenDevice == "cuda"
        assert controller.setQwenVariant("gguf", "Q8_0") is True
        # The GGUF picker shows ITS OWN stored preference, in ggml names.
        assert controller.qwenDevice == "auto"
        assert controller.setQwenDevice("metal") is True
        assert controller.qwenDevice == "metal"
        saved = harness.read_settings()
        assert saved["qwen_device"] == "cuda"  # untouched by the GGUF pick
        assert saved["qwen_gguf_device"] == "metal"
        assert controller.setQwenVariant("official", "") is True
        assert controller.qwenDevice == "cuda"  # restored, not reset

    def test_device_options_speak_the_selected_engines_vocabulary(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        harness = _both_ready(tmp_path)
        controller = harness.controller
        official = [o["value"] for o in controller.qwenDeviceOptions]
        assert official == ["auto", "cpu", "cuda", "mps"]
        assert controller.setQwenVariant("gguf", "Q8_0") is True
        gguf = [o["value"] for o in controller.qwenDeviceOptions]
        assert gguf == ["auto", "cpu", "cuda", "metal"]  # Metal, never mps

    def test_gguf_auto_resolves_a_validated_backend(self, qcoreapp, tmp_path: Path) -> None:
        # linux-x64 ships only the cpu pack today: on an NVIDIA host `auto`
        # must NOT resolve to a cuda cell no published pack exists for — the
        # resolved device is cpu, and the runtime key the card reports is the
        # installable linux-x64-cpu cell.
        write_settings_file(tmp_path, qwen_model_format="gguf", qwen_gguf_device="auto")
        harness = ProfileHarness.qwen_gguf_ready(tmp_path, hardware=NVIDIA)
        controller = harness.controller
        controller.refreshProfileState()
        options = {o["value"]: o for o in controller.qwenDeviceOptions}
        assert options["auto"]["resolved"] == "cpu"
        assert options["cuda"]["supported"] is False  # no published pack
        assert options["metal"]["supported"] is False  # no such cell on linux
        assert controller.qwenRuntimePlatformKey == "linux-x64-cpu"
        # The ACTIVE Qwen profile's device resolves through ggml vocabulary.
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        assert controller.engineDevice == "cpu"

    def test_explicit_gguf_device_reports_its_cell(self, qcoreapp, tmp_path: Path) -> None:
        write_settings_file(tmp_path, qwen_model_format="gguf", qwen_gguf_device="cuda")
        harness = ProfileHarness.qwen_gguf_ready(tmp_path, hardware=NVIDIA)
        controller = harness.controller
        controller.refreshProfileState()
        assert controller.qwenDevice == "cuda"
        # The cell exists in the matrix but ships no manifest yet — the card
        # is truthful: unsupported, not "ready".
        assert controller.qwenRuntimePlatformKey == "linux-x64-cuda"
        assert controller.qwenRuntimeSupported is False

    def test_set_device_rejects_the_other_formats_vocabulary(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        harness = ProfileHarness(tmp_path)
        controller = harness.controller
        assert controller.setQwenVariant("gguf", "Q8_0") is True
        assert controller.setQwenDevice("mps") is False  # PyTorch name
        assert controller.setQwenDevice("metal") is True
        assert controller.setQwenVariant("official", "") is True
        assert controller.setQwenDevice("metal") is False  # ggml name
        assert controller.qwenDevice == "auto"


class TestVariantAwareReadiness:
    """Inspection resolves the whole selected variant — runtime cell + models."""

    def test_gguf_inspection_publishes_the_cell_and_variants(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        controller.refreshProfileState()
        assert controller.qwenRuntimeState == "ready"
        assert controller.qwenRuntimeReady is True
        assert controller.qwenRuntimePlatformKey == "linux-x64-cpu"
        assert controller.qwenRuntimeVariantLabel != ""
        rows = {row["key"]: row for row in controller.qwenModels}
        assert set(rows) == {
            "customvoice-Q8_0",
            "customvoice-Q4_K_M",
            "base-Q8_0",
            "base-Q4_K_M",
        }
        assert rows["customvoice-Q8_0"]["ready"] is True
        assert rows["customvoice-Q8_0"]["format"] == "gguf"
        assert rows["customvoice-Q8_0"]["quantization"] == "Q8_0"
        assert rows["customvoice-Q8_0"]["engine"] == "qwentts_cpp"
        assert rows["customvoice-Q8_0"]["isSelected"] is True
        assert rows["customvoice-Q4_K_M"]["isSelected"] is False

    def test_unsupported_gguf_host_reports_unsupported(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path, gguf_runtime_supported=False)
        controller = harness.controller
        controller.refreshProfileState()
        assert controller.qwenRuntimeState == "unsupported"
        assert controller.qwenRuntimeReady is False

    def test_missing_gguf_model_is_unavailable_even_when_official_is_ready(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        # No silent fallback: the official install being ready must not make
        # a GGUF selection report ready.
        _gguf_settings(tmp_path)
        harness = ProfileHarness(
            tmp_path,
            model_status=QwenModelStatus(state="ready", location=qwen_model_location(tmp_path)),
            runtime_status=QwenRuntimeStatus(
                state="ready", location=qwen_runtime_location(tmp_path)
            ),
            gguf_runtime_status=QwenGgufRuntimeStatus(
                state="ready", location=qwen_gguf_runtime_location(tmp_path)
            ),
            gguf_model_status=QwenGgufModelStatus(state="unavailable"),
        )
        controller = harness.controller
        controller.refreshProfileState()
        assert controller.qwenRuntimeReady is True  # the RUNTIME is ready
        rows = {row["key"]: row for row in controller.qwenModels}
        assert rows["customvoice-Q8_0"]["ready"] is False
        assert rows["customvoice-Q8_0"]["state"] == "unavailable"
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        assert controller.profileReady is False  # the SELECTED variant gates
        # …and the submission fails at engine build with the GGUF reason.
        controller.generate("你好", "Vivian")
        assert harness.qwen_gguf_engines == []
        assert harness.qwen_engines == []  # never fell back to PyTorch
        assert "not installed" in controller.errorText

    def test_quantization_switch_rekeys_the_model_rows(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        controller.refreshProfileState()
        rows = {row["key"]: row for row in controller.qwenModels}
        assert rows["customvoice-Q8_0"]["isSelected"] is True
        assert controller.setQwenVariant("gguf", "Q4_K_M") is True
        controller.refreshProfileState()
        rows = {row["key"]: row for row in controller.qwenModels}
        assert rows["customvoice-Q4_K_M"]["isSelected"] is True
        assert rows["customvoice-Q8_0"]["isSelected"] is False
        # Both quantizations stay in the matrix — installs are per-variant.
        assert set(rows) == {
            "customvoice-Q8_0",
            "customvoice-Q4_K_M",
            "base-Q8_0",
            "base-Q4_K_M",
        }


class TestSwitchRefusals:
    """The variant — like the profile — only changes while the engine is idle."""

    def test_switch_refused_while_a_job_is_running(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness.qwen_ready(tmp_path)
        controller = harness.controller
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        controller.generate("你好", "Vivian")
        assert controller.busy is True
        assert controller.setQwenVariant("gguf", "Q8_0") is False
        assert controller.qwenModelFormat == "official"
        # Once the job ends the same switch is allowed.
        artifact = make_artifact(tmp_path / "done.wav", harness.worker.submitted[-1].id)
        harness.worker.complete_last(artifact)
        assert controller.setQwenVariant("gguf", "Q8_0") is True

    def test_switch_refused_while_work_is_queued(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness.qwen_ready(tmp_path)
        controller = harness.controller
        controller.generate("hi", "")
        harness.worker.complete_last(
            make_artifact(tmp_path / "a.wav", harness.worker.submitted[-1].id)
        )
        harness.worker.pending_work = True  # e.g. an audiobook batch owns the queue
        assert controller.setQwenVariant("gguf", "Q8_0") is False
        assert controller.qwenModelFormat == "official"

    def test_switch_refused_while_cancelling(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness.qwen_ready(tmp_path)
        controller = harness.controller
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        controller.generate("你好", "Vivian")
        controller._set_foreground_job_state("cancel_requested")
        assert controller.setQwenVariant("gguf", "Q8_0") is False
        assert controller.qwenModelFormat == "official"

    def test_switch_refused_while_an_install_owns_the_lane(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness(tmp_path, deferred=True)
        harness.pending.clear()
        controller = harness.controller
        controller.installQwenRuntime()
        assert controller.qwenRuntimeBusy is True
        assert controller.setQwenVariant("gguf", "Q8_0") is False
        assert controller.qwenModelFormat == "official"
        harness.run_pending(0)  # the install lands; the lane frees
        assert controller.setQwenVariant("gguf", "Q8_0") is True

    def test_switch_while_idle_retires_the_built_engine(self, qcoreapp, tmp_path: Path) -> None:
        # "change the engine only when idle": an accepted switch tears down the
        # live engine so the NEXT submission builds the new variant — a job can
        # never start on an engine whose variant no longer matches the context.
        write_settings_file(
            tmp_path,
            qwen_model_format="official",
            qwen_gguf_quantization="Q8_0",
            qwen_gguf_device="cpu",
        )
        harness = _both_ready(tmp_path)
        controller = harness.controller
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        controller.generate("你好", "Vivian")
        (old,) = harness.qwen_engines
        harness.worker.complete_last(
            make_artifact(tmp_path / "a.wav", harness.worker.submitted[-1].id)
        )
        assert controller.setQwenVariant("gguf", "Q4_K_M") is True
        assert old.closed is True  # the PyTorch engine was torn down
        controller.generate("你好", "Vivian")
        (new,) = harness.qwen_gguf_engines
        assert new.kwargs["quantization"] == "Q4_K_M"
        assert new.kwargs["device"] == "cpu"
        job = harness.worker.submitted[-1]
        assert job.context.engine == "qwentts_cpp"
        assert job.context.quantization == "Q4_K_M"


class TestStaleResults:
    """Generation guards: a result for a superseded selection never lands."""

    def test_stale_inspection_result_is_dropped(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness.qwen_gguf_ready(tmp_path, deferred=True)
        harness.pending.clear()  # anything post-paint queued
        controller = harness.controller
        controller.refreshQwenState()  # official-selection inspection, queued
        assert len(harness.pending) == 1
        assert controller.setQwenVariant("gguf", "Q8_0") is True
        assert len(harness.pending) == 2  # a GGUF inspection was queued
        harness.run_pending(0)  # the stale official result lands late
        assert controller.qwenRuntimePlatformKey == ""  # nothing was published
        harness.run_pending(0)
        assert controller.qwenRuntimePlatformKey == "linux-x64-cpu"
        assert controller.qwenRuntimeState == "ready"

    def test_stale_install_result_is_dropped(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness(tmp_path, deferred=True)
        harness.pending.clear()
        controller = harness.controller
        controller.installQwenModel("customvoice")
        assert len(harness.pending) == 1
        controller.cancelQwenModelDownload("customvoice")
        assert controller.setQwenVariant("gguf", "Q8_0") is True
        harness.run_pending(0)  # the cancelled install's result lands late
        status = controller._qwen_model_statuses.get("customvoice")
        assert status is None or status.state != "ready"
        harness.run_pending(0)  # the GGUF inspection still lands
        assert controller.qwenRuntimePlatformKey == "linux-x64-cpu"


class TestVariantInstallIdentity:
    """Install/remove/import operations address the exact keyed variant."""

    def test_model_install_targets_the_keyed_variant(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(
            tmp_path,
            gguf_model_status=QwenGgufModelStatus(state="unavailable"),
        )
        controller = harness.controller
        # Q8_0 is selected, but the key names Q4_K_M — the call installs the
        # variant the KEY describes, not whatever the selection resolves to.
        controller.installQwenModel("customvoice-Q4_K_M")
        manager = _gguf_model_manager(harness, "customvoice-Q4_K_M")
        assert ("install", "started") in manager.calls
        assert _gguf_model_manager(harness, "customvoice-Q8_0").calls == []
        row = {r["key"]: r for r in controller.qwenModels}["customvoice-Q4_K_M"]
        assert row["ready"] is True

    def test_official_model_key_still_targets_the_official_install(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        controller.installQwenModel("base")
        manager = harness.manager_for("base")
        assert ("install", "started") in manager.calls
        assert all(m.calls == [] for m in harness.gguf_model_managers)

    def test_runtime_install_targets_the_gguf_cell(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness(
            tmp_path,
            gguf_runtime_status=QwenGgufRuntimeStatus(state="unavailable"),
        )
        controller = harness.controller
        controller.installQwenRuntime()
        manager = harness.gguf_runtime_managers[0]
        assert manager.cell == "linux-x64-cpu"
        assert ("install_online", "started") in manager.calls
        assert controller.qwenRuntimeState == "ready"
        assert harness.runtime_managers == []  # the official lane was untouched

    def test_runtime_repair_and_import_dispatch_to_the_gguf_pack(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        controller.repairQwenRuntime()
        manager = harness.gguf_runtime_managers[0]
        assert ("repair", "started") in manager.calls
        pack = tmp_path / "pack"
        pack.mkdir()
        controller.importQwenRuntimePack(str(pack))
        assert ("offline", "started") in manager.calls
        assert manager.offline_dirs == [pack]

    def test_model_import_dispatches_to_install_offline(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        source = tmp_path / "model-pack"
        source.mkdir()
        controller.importQwenModelPack("base-Q8_0", str(source))
        manager = _gguf_model_manager(harness, "base-Q8_0")
        assert ("install_offline", "started") in manager.calls
        assert manager.offline_dirs == [source]

    def test_remove_gguf_model_passes_drop_shared(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        controller.removeQwenModel("customvoice-Q8_0")
        manager = _gguf_model_manager(harness, "customvoice-Q8_0")
        (call,) = manager.calls
        assert call[0] == "remove"
        assert "in_use=False" in call[1]
        # Nothing else installed on disk → the shared codec may go.
        assert "drop_shared=True" in call[1]

    def test_remove_refuses_the_variant_a_live_engine_holds(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        controller.generate("你好", "Vivian")
        # The live GGUF engine holds customvoice-Q8_0 — its sibling quant and
        # the other profile stay removable, the in-use variant is refused.
        controller.removeQwenModel("customvoice-Q4_K_M")
        sibling = _gguf_model_manager(harness, "customvoice-Q4_K_M")
        assert sibling.calls[0][0] == "remove"
        assert "in_use=False" in sibling.calls[0][1]
        controller.removeQwenModel("customvoice-Q8_0")
        live = _gguf_model_manager(harness, "customvoice-Q8_0")
        assert "in_use=True" in live.calls[0][1]
        assert live.status.state == "failed"  # the manager refused the remove

    def test_unknown_variant_keys_are_refused(self, qcoreapp, tmp_path: Path) -> None:
        harness = ProfileHarness(tmp_path)
        controller = harness.controller
        controller.installQwenModel("customvoice-Q2_K")
        assert controller.errorText != ""
        controller.installQwenModel("nonsense")
        assert controller.errorText != ""


class TestByteCountersStayQlonglong:
    """QML-facing counters remain 64-bit under either format."""

    def test_counters_are_qlonglong_and_carry_gguf_bytes(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        controller.refreshProfileState()
        meta = controller.metaObject()
        types = {}
        for i in range(meta.propertyCount()):
            prop = meta.property(i)
            name = prop.name()
            types[name if isinstance(name, str) else bytes(name).decode()] = prop.typeName()
        for name in (
            "qwenRuntimeInstalledBytes",
            "qwenRuntimeRequiredBytes",
            "qwenSharedBytes",
        ):
            assert types.get(name) == "qlonglong", f"{name} is {types.get(name)}"
        # The published linux-x64-cpu pack is ~18 MB — a 32-bit int would
        # hold it, but the contract is 64-bit so QML never overflows.
        assert controller.qwenRuntimeRequiredBytes > 0
        assert controller.qwenSharedBytes > 0


class TestJobIdentityAgreement:
    """Status, Generate gating, engine factory and job identity all agree."""

    def test_context_engine_and_factory_match_the_selection(self, qcoreapp, tmp_path: Path) -> None:
        _gguf_settings(tmp_path)
        harness = ProfileHarness.qwen_gguf_ready(tmp_path)
        controller = harness.controller
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        context = controller.submission_context_for("Vivian")
        assert context is not None
        assert context.engine == "qwentts_cpp"
        assert context.quantization == "Q8_0"
        controller.generate("你好", "Vivian")
        (engine,) = harness.qwen_gguf_engines
        job = harness.worker.submitted[-1]
        assert job.context.engine == "qwentts_cpp"
        assert engine.kwargs["quantization"] == "Q8_0"
        assert harness.qwen_engines == []  # the PyTorch lane never ran

    def test_official_selection_never_builds_the_native_engine(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        write_settings_file(tmp_path, qwen_model_format="official")
        harness = _both_ready(tmp_path)
        controller = harness.controller
        assert controller.switchEngineProfile(QWEN_CUSTOM) is True
        controller.generate("你好", "Vivian")
        assert len(harness.qwen_engines) == 1
        assert harness.qwen_gguf_engines == []
        job = harness.worker.submitted[-1]
        assert job.context.engine == "pytorch"
