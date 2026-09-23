"""Qwen model-format variant contract (track qwen_gguf_engine_20260923).

``core/qwen_variants`` describes one selection dimension of the existing Qwen
profiles: *Official full weights* (PyTorch host) vs GGUF (qwentts.cpp host) at
exactly ``Q8_0`` / ``Q4_K_M``.  These tests pin that variants never mint new
profile IDs, engines resolve from format, and device vocabularies stay
engine-scoped (native ``Metal`` never masquerades as PyTorch ``mps``).
"""

from __future__ import annotations

import pytest

from vienetts_app.core import engine_profiles as ep
from vienetts_app.core import qwen_variants as qv
from vienetts_app.core.models import Settings


class TestVariantFor:
    def test_official_is_the_default_format(self) -> None:
        variant = qv.variant_for(ep.QWEN_BASE)
        assert (variant.profile, variant.model_format) == (ep.QWEN_BASE, "official")
        assert variant.engine == "pytorch"
        assert variant.quantization == ""

    def test_official_customvoice(self) -> None:
        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="official")
        assert (variant.profile, variant.engine) == (ep.QWEN_CUSTOM, "pytorch")
        assert variant.quantization == ""

    def test_gguf_variants_resolve_to_qwentts_cpp(self) -> None:
        for profile in (ep.QWEN_BASE, ep.QWEN_CUSTOM):
            for quant in ("Q8_0", "Q4_K_M"):
                variant = qv.variant_for(profile, model_format="gguf", quantization=quant)
                assert (variant.profile, variant.engine) == (profile, "qwentts_cpp")
                assert variant.quantization == quant

    def test_gguf_empty_quantization_defaults_to_q8(self) -> None:
        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf")
        assert variant.quantization == "Q8_0"

    def test_official_with_quantization_is_rejected(self) -> None:
        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.QWEN_BASE, model_format="official", quantization="Q8_0")

    def test_gguf_with_unknown_quantization_is_rejected(self) -> None:
        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="F16")

    def test_unknown_format_is_rejected(self) -> None:
        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.QWEN_BASE, model_format="onnx")

    def test_vieneu_has_no_variants(self) -> None:
        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.VIENEU)

    def test_unknown_profile_is_rejected(self) -> None:
        with pytest.raises(qv.VariantError):
            qv.variant_for("nope")  # type: ignore[arg-type]

    def test_variants_are_immutable(self) -> None:
        variant = qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="Q4_K_M")
        with pytest.raises(AttributeError):
            variant.engine = "other"  # type: ignore[misc]


class TestDeviceVocabulary:
    def test_official_devices_keep_mps(self) -> None:
        variant = qv.variant_for(ep.QWEN_CUSTOM)
        assert variant.devices == ("cpu", "cuda", "mps")

    def test_gguf_devices_are_native_names(self) -> None:
        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf")
        assert variant.devices == ("cpu", "cuda", "metal")
        assert "mps" not in variant.devices

    def test_metal_never_appears_on_the_official_path(self) -> None:
        assert "metal" not in qv.variant_for(ep.QWEN_BASE).devices

    def test_device_labels_distinguish_native_metal_from_mps(self) -> None:
        assert qv.device_label("metal") == "Metal"
        assert qv.device_label("mps") == "MPS"
        assert qv.device_label("cpu") == "CPU"
        assert qv.device_label("cuda") == "CUDA"
        assert qv.device_label("auto") == "Auto"


class TestCapabilities:
    def test_variant_capabilities_are_the_shared_profile_table(self) -> None:
        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf")
        assert variant.capabilities is ep.get_capabilities(ep.QWEN_CUSTOM)

    def test_customvoice_gguf_keeps_all_nine_speakers(self) -> None:
        caps = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf").capabilities
        assert len(caps.voices) == 9
        assert caps.voices == ep.QWEN_SPEAKERS

    def test_base_gguf_keeps_clone_requirements(self) -> None:
        caps = qv.variant_for(ep.QWEN_BASE, model_format="gguf").capabilities
        assert caps.supports_cloning
        assert caps.clone_requirements == ("reference_clip", "transcript", "consent")


class TestResolveVariant:
    def test_default_settings_resolve_to_official(self) -> None:
        variant = qv.resolve_variant(Settings(engine_profile=ep.QWEN_BASE))
        assert variant == qv.variant_for(ep.QWEN_BASE)

    def test_gguf_settings_resolve(self) -> None:
        settings = Settings(
            engine_profile=ep.QWEN_CUSTOM,
            qwen_model_format="gguf",
            qwen_gguf_quantization="Q4_K_M",
        )
        variant = qv.resolve_variant(settings)
        assert (variant.engine, variant.quantization) == ("qwentts_cpp", "Q4_K_M")

    def test_vieneu_profile_resolves_to_none(self) -> None:
        assert qv.resolve_variant(Settings(engine_profile=ep.VIENEU)) is None


class TestSettingsContract:
    def test_new_fields_have_official_defaults(self) -> None:
        settings = Settings()
        assert settings.qwen_model_format == "official"
        assert settings.qwen_gguf_quantization == "Q8_0"
        assert settings.qwen_gguf_device == "auto"

    def test_settings_reject_unknown_format(self) -> None:
        with pytest.raises(ValueError):
            Settings(qwen_model_format="onnx")

    def test_settings_reject_unknown_quantization(self) -> None:
        with pytest.raises(ValueError):
            Settings(qwen_gguf_quantization="F16")

    def test_settings_reject_mps_on_the_gguf_device(self) -> None:
        with pytest.raises(ValueError):
            Settings(qwen_gguf_device="mps")

    def test_settings_reject_metal_on_the_official_device(self) -> None:
        with pytest.raises(ValueError):
            Settings(qwen_device="metal")

    def test_gguf_device_accepts_metal(self) -> None:
        settings = Settings(qwen_gguf_device="metal")
        assert settings.qwen_gguf_device == "metal"
