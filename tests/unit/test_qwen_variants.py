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
    def test_official_variant_resolution(self) -> None:
        variant = qv.variant_for(ep.QWEN_BASE)
        assert (variant.profile, variant.model_format) == (ep.QWEN_BASE, "official")
        assert variant.engine == "pytorch"
        assert variant.quantization == ""

        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="official")
        assert (variant.profile, variant.engine) == (ep.QWEN_CUSTOM, "pytorch")
        assert variant.quantization == ""

        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf")
        assert variant.quantization == "Q8_0"

    def test_variant_rejections_and_vieneu(self) -> None:
        for profile in (ep.QWEN_BASE, ep.QWEN_CUSTOM):
            for quant in ("Q8_0", "Q4_K_M"):
                variant = qv.variant_for(profile, model_format="gguf", quantization=quant)
                assert (variant.profile, variant.engine) == (profile, "qwentts_cpp")
                assert variant.quantization == quant

        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.QWEN_BASE, model_format="official", quantization="Q8_0")

        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="F16")

        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.QWEN_BASE, model_format="onnx")

        with pytest.raises(qv.VariantError):
            qv.variant_for(ep.VIENEU)

        with pytest.raises(qv.VariantError):
            qv.variant_for("nope")  # type: ignore[arg-type]


class TestDeviceVocabulary:
    def test_device_vocabulary_contract(self) -> None:
        variant = qv.variant_for(ep.QWEN_CUSTOM)
        assert variant.devices == ("cpu", "cuda", "mps")

        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf")
        assert variant.devices == ("cpu", "cuda", "metal")
        assert "mps" not in variant.devices

        assert "metal" not in qv.variant_for(ep.QWEN_BASE).devices

        assert qv.device_label("metal") == "Metal"
        assert qv.device_label("mps") == "MPS"


class TestCapabilities:
    def test_variant_capabilities_contract(self) -> None:
        variant = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf")
        assert variant.capabilities is ep.get_capabilities(ep.QWEN_CUSTOM)

        caps = qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf").capabilities
        assert caps.voices == ep.QWEN_SPEAKERS

        caps = qv.variant_for(ep.QWEN_BASE, model_format="gguf").capabilities
        assert caps.supports_cloning
        assert caps.clone_requirements == ("reference_clip", "transcript", "consent")


class TestResolveVariant:
    def test_resolve_variant_contract(self) -> None:
        # The app's default format for the Qwen family is GGUF: a fresh
        # install (no stored choice) resolves to the native engine at Q8_0.
        variant = qv.resolve_variant(Settings(engine_profile=ep.QWEN_BASE))
        assert variant == qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="Q8_0")

        settings = Settings(engine_profile=ep.QWEN_BASE, qwen_model_format="official")
        variant = qv.resolve_variant(settings)
        assert variant == qv.variant_for(ep.QWEN_BASE, model_format="official")

        settings = Settings(
            engine_profile=ep.QWEN_CUSTOM,
            qwen_model_format="gguf",
            qwen_gguf_quantization="Q4_K_M",
        )
        variant = qv.resolve_variant(settings)
        assert (variant.engine, variant.quantization) == ("qwentts_cpp", "Q4_K_M")

        assert qv.resolve_variant(Settings(engine_profile=ep.VIENEU)) is None


class TestSettingsContract:
    def test_variant_settings_contract(self) -> None:
        """Defaults plus the unknown-format/quantization rejections in one node."""
        settings = Settings()
        # GGUF is the Qwen family's default format (native pack + one talker
        # instead of a multi-GB PyTorch runtime plus full weights); the
        # device preferences stay on auto.
        assert settings.qwen_model_format == "gguf", "default format"
        assert settings.qwen_gguf_quantization == "Q8_0", "default quantization"
        assert settings.qwen_gguf_device == "auto", "default device"
        for label, kwargs in (
            ("unknown format", {"qwen_model_format": "onnx"}),
            ("unknown quantization", {"qwen_gguf_quantization": "F16"}),
        ):
            try:
                Settings(**kwargs)  # noqa: B008 — construction is the assertion
            except ValueError:
                continue
            raise AssertionError(f"{label}: Settings should have rejected {kwargs}")

        with pytest.raises(ValueError):
            Settings(qwen_gguf_device="mps")

        with pytest.raises(ValueError):
            Settings(qwen_device="metal")

        settings = Settings(qwen_gguf_device="metal")
        assert settings.qwen_gguf_device == "metal"
