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


# --- Task 2.4: Managed runtime & Qwen model hardening ------------------------


def _make_wheel_bytes(*members: tuple[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


class _FakeHttpResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status: int = 200,
        url: str,
        headers: dict[str, str] | None = None,
        fail_after_first_chunk: Exception | None = None,
    ) -> None:
        import io

        self._buf = io.BytesIO(content)
        self.status = status
        self.url = url
        self.headers = dict(headers or {})
        self._fail_exc = fail_after_first_chunk
        self._chunk_sent = False

    def read(self, size: int = -1) -> bytes:
        if self._fail_exc is not None and self._chunk_sent:
            raise self._fail_exc
        data = self._buf.read(size)
        if data:
            self._chunk_sent = True
        return data

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        return None

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class TestCudaRuntimeManagerHardening:
    @staticmethod
    def _manifest():
        import hashlib
        import sys

        from vienetts_app.core.cuda_runtime_manifest import CudaRuntimeManifest, RuntimeWheel

        py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
        whl_bytes = _make_wheel_bytes(
            ("torch/__init__.py", b"# fake torch\n"),
            ("torch/_C.so", b"native"),
            ("torch/lib/libtorch_cuda.so", b"cuda"),
            ("nvidia/cuda_runtime/lib/libcudart.so", b"cudart"),
        )
        wheel = RuntimeWheel(
            filename=f"torch-2.8.0+cu128-{py_tag}-{py_tag}-manylinux_2_28_x86_64.whl",
            url=f"https://download.pytorch.org/whl/cu128/torch-2.8.0+cu128-{py_tag}-{py_tag}-manylinux_2_28_x86_64.whl",
            size_bytes=len(whl_bytes),
            sha256=hashlib.sha256(whl_bytes).hexdigest(),
        )
        manifest = CudaRuntimeManifest(
            format_version="cuda-cu128-v1",
            platform_key="linux-x64",
            python_tag=py_tag,
            wheels=(wheel,),
        )
        return manifest, wheel, whl_bytes

    def test_wheel_cache_outside_staging_inline_sha_free_space_and_legacy_pruning(
        self, tmp_path, monkeypatch
    ) -> None:
        from vienetts_app.core import managed_install as mi
        from vienetts_app.core.cuda_runtime import CudaRuntimeManager

        manifest, wheel, whl_bytes = self._manifest()
        hash_calls: list[object] = []
        real_hash = mi.sha256_of
        monkeypatch.setattr(mi, "sha256_of", lambda p: hash_calls.append(p) or real_hash(p))

        def opener(request, timeout: float = 30) -> _FakeHttpResponse:
            return _FakeHttpResponse(
                whl_bytes,
                url=request.full_url,
                headers={"Content-Length": str(len(whl_bytes))},
            )

        class TightDisk:
            free = len(whl_bytes) * 3

        class PlentyDisk:
            free = len(whl_bytes) * 10

        tight_mgr = CudaRuntimeManager(
            tmp_path / "tight", manifest, opener=opener, disk_usage=lambda _p: TightDisk()
        )
        assert tight_mgr._required_free_bytes >= int(len(whl_bytes) * 3.5)
        assert not tight_mgr._archive_path(wheel).is_relative_to(tight_mgr._staging_dir())
        assert tight_mgr.install().state == "failed"

        manager = CudaRuntimeManager(
            tmp_path / "rt", manifest, opener=opener, disk_usage=lambda _p: PlentyDisk()
        )
        status = manager.install()
        assert status.state == "ready", status.error
        # Fresh offset-0 HTTP stream validates via inline SHA-256 with 0 sha256_of reads.
        assert hash_calls == []
        # Wheel cache was never promoted into active/wheels and was deleted after promotion.
        active = tmp_path / "rt" / manifest.format_version
        assert not (active / "wheels").exists()
        assert not manager._archive_path(wheel).parent.exists()

        # Legacy active/wheels/ is proactively pruned on inspect().
        legacy_wheel = active / "wheels" / "torch.whl.part"
        legacy_wheel.parent.mkdir(parents=True, exist_ok=True)
        legacy_wheel.write_bytes(b"legacy-3gb-leak")
        assert manager.inspect().state == "ready"
        assert not (active / "wheels").exists()

        # Legacy active/wheels/ is also pruned on install() over a broken legacy install.
        (active / "install.json").unlink()
        legacy_wheel.parent.mkdir(parents=True, exist_ok=True)
        legacy_wheel.write_bytes(b"legacy-leak-2")
        assert manager.install().state == "ready"
        assert not (active / "wheels").exists()

        # Strict remove fails honestly if active stays locked.
        monkeypatch.setattr(mi, "REMOVAL_RETRY_SECONDS", 0.0)
        monkeypatch.setattr(mi.shutil, "rmtree", lambda *_a, **_k: None)
        removed = manager.remove()
        assert removed.state == "failed"
        assert "could not remove" in removed.error

    def test_transient_retry_cancel_partial_content_length_and_checksum_unlink(
        self, tmp_path, monkeypatch
    ) -> None:
        import http.client

        from vienetts_app.core import cuda_runtime as cuda_mod
        from vienetts_app.core.cuda_runtime import CudaRuntimeManager

        monkeypatch.setattr(cuda_mod, "_DOWNLOAD_BACKOFF_BASE_SECONDS", 0.0)
        manifest, wheel, whl_bytes = self._manifest()

        # 1) Transient IncompleteRead keeps partial < size_bytes and resumes via 206.
        seen_ranges: list[str | None] = []

        def flaky_opener(request, timeout: float = 30) -> _FakeHttpResponse:
            rng = request.headers.get("Range")
            seen_ranges.append(rng)
            if len(seen_ranges) == 1:
                return _FakeHttpResponse(
                    whl_bytes[:10],
                    url=request.full_url,
                    fail_after_first_chunk=http.client.IncompleteRead(b"partial"),
                )
            assert rng == "bytes=10-"
            return _FakeHttpResponse(
                whl_bytes[10:],
                status=206,
                url=request.full_url,
                headers={"Content-Range": f"bytes 10-{len(whl_bytes) - 1}/{len(whl_bytes)}"},
            )

        mgr = CudaRuntimeManager(tmp_path / "retry", manifest, opener=flaky_opener)
        assert mgr.install().state == "ready"
        assert seen_ranges == [None, "bytes=10-"]

        # 2) Cancel keeps partial < size_bytes on disk for resume.
        cancel_flag = {"now": False}

        def cancel_opener(request, timeout: float = 30) -> _FakeHttpResponse:
            rng = request.headers.get("Range")
            if rng:
                offset = int(rng.removeprefix("bytes=").split("-")[0])
                return _FakeHttpResponse(
                    whl_bytes[offset:],
                    status=206,
                    url=request.full_url,
                    headers={
                        "Content-Range": f"bytes {offset}-{len(whl_bytes) - 1}/{len(whl_bytes)}"
                    },
                )

            class ChunkingResp(_FakeHttpResponse):
                def read(self, size: int = -1) -> bytes:
                    data = self._buf.read(16)
                    if data:
                        cancel_flag["now"] = True
                    return data

            return ChunkingResp(whl_bytes, url=request.full_url)

        cancel_mgr = CudaRuntimeManager(tmp_path / "cancel", manifest, opener=cancel_opener)
        first = cancel_mgr.install(cancelled=lambda: cancel_flag["now"])
        assert first.state == "unavailable"
        part = cancel_mgr._archive_path(wheel)
        assert part.is_file() and 0 < part.stat().st_size < wheel.size_bytes
        cancel_flag["now"] = False
        assert cancel_mgr.install().state == "ready"

        # 3) Bad/negative Content-Length rejected cleanly.
        for bad_cl in ("abc", "-1"):
            cl_mgr = CudaRuntimeManager(
                tmp_path / f"cl-{bad_cl}",
                manifest,
                opener=lambda req, timeout=30, cl=bad_cl: _FakeHttpResponse(
                    whl_bytes, url=req.full_url, headers={"Content-Length": cl}
                ),
            )
            res = cl_mgr.install()
            assert res.state == "failed"
            assert "Content-Length" in res.error or "download failed" in res.error

        # 4) Checksum mismatch unlinks corrupt wheel archive.
        corrupt_mgr = CudaRuntimeManager(
            tmp_path / "corrupt",
            manifest,
            opener=lambda req, timeout=30: _FakeHttpResponse(
                b"x" * len(whl_bytes), url=req.full_url
            ),
        )
        res = corrupt_mgr.install()
        assert res.state == "failed"
        assert "checksum mismatch" in res.error
        assert not corrupt_mgr._archive_path(wheel).exists()


class TestQwenRuntimeManagerHardening:
    @staticmethod
    def _manifest():
        import hashlib
        import sys

        from vienetts_app.core.managed_install import RuntimeWheel
        from vienetts_app.core.qwen_runtime_manifest import QwenRuntimeManifest

        py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
        whl_bytes = _make_wheel_bytes(("qwen_tts/__init__.py", b"__version__ = '0.1'\n"))
        wheel = RuntimeWheel(
            filename="qwen_tts-0.1.0-py3-none-any.whl",
            url="https://files.pythonhosted.org/packages/aa/bb/qwen_tts-0.1.0-py3-none-any.whl",
            size_bytes=len(whl_bytes),
            sha256=hashlib.sha256(whl_bytes).hexdigest(),
        )
        manifest = QwenRuntimeManifest(
            format_version="qwen-runtime-v1",
            platform_key="linux-x64-cpu",
            platform_tag="linux_x86_64",
            device="cpu",
            python_tag=py_tag,
            torch_local_version="2.8.0+cpu",
            pins={"qwen-tts": "0.1.0"},
            wheels=(wheel,),
        )
        return manifest, wheel, whl_bytes

    def test_wheel_cache_outside_staging_inline_sha_free_space_and_legacy_pruning(
        self, tmp_path, monkeypatch
    ) -> None:
        from vienetts_app.core import managed_install as mi
        from vienetts_app.core.qwen_runtime import QwenRuntimeManager

        manifest, wheel, whl_bytes = self._manifest()
        hash_calls: list[object] = []
        real_hash = mi.sha256_of
        monkeypatch.setattr(mi, "sha256_of", lambda p: hash_calls.append(p) or real_hash(p))

        def opener(request, timeout: float = 30) -> _FakeHttpResponse:
            return _FakeHttpResponse(
                whl_bytes,
                url=request.full_url,
                headers={"Content-Length": str(len(whl_bytes))},
            )

        class TightDisk:
            free = len(whl_bytes) * 3

        class PlentyDisk:
            free = len(whl_bytes) * 10

        tight = QwenRuntimeManager(
            tmp_path / "tight",
            manifest,
            opener=opener,
            disk_usage=lambda _p: TightDisk(),
            import_check=lambda _p: (True, ""),
        )
        assert tight._required_free_bytes >= int(len(whl_bytes) * 3.5)
        assert not tight._archive_path(wheel).is_relative_to(tight._staging_dir())
        assert tight.install().state == "failed"

        manager = QwenRuntimeManager(
            tmp_path / "rt",
            manifest,
            opener=opener,
            disk_usage=lambda _p: PlentyDisk(),
            import_check=lambda _p: (True, ""),
        )
        status = manager.install()
        assert status.state == "ready", status.error
        assert hash_calls == []
        active = tmp_path / "rt" / manifest.format_version
        assert not (active / "wheels").exists()
        assert not manager._archive_path(wheel).parent.exists()

        # Legacy active/wheels/ pruned on inspect() and repair().
        legacy = active / "wheels" / "torch.whl.part"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"old-wheels")
        assert manager.inspect().state == "ready"
        assert not (active / "wheels").exists()

        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"old-wheels")
        assert manager.repair().state == "ready"
        assert not (active / "wheels").exists()

        # Strict remove reports failure when active dir cannot be deleted.
        monkeypatch.setattr(mi, "REMOVAL_RETRY_SECONDS", 0.0)
        monkeypatch.setattr(mi.shutil, "rmtree", lambda *_a, **_k: None)
        removed = manager.remove()
        assert removed.state == "failed"
        assert "could not remove" in removed.error

    def test_transient_retry_cancel_partial_and_checksum_unlink(
        self, tmp_path, monkeypatch
    ) -> None:
        import http.client

        from vienetts_app.core import qwen_runtime as qr_mod
        from vienetts_app.core.qwen_runtime import QwenRuntimeManager

        monkeypatch.setattr(qr_mod, "_DOWNLOAD_BACKOFF_BASE_SECONDS", 0.0)
        manifest, wheel, whl_bytes = self._manifest()

        seen_ranges: list[str | None] = []

        def flaky_opener(request, timeout: float = 30) -> _FakeHttpResponse:
            rng = request.headers.get("Range")
            seen_ranges.append(rng)
            if len(seen_ranges) == 1:
                return _FakeHttpResponse(
                    whl_bytes[:12],
                    url=request.full_url,
                    fail_after_first_chunk=http.client.IncompleteRead(b"partial"),
                )
            return _FakeHttpResponse(
                whl_bytes[12:],
                status=206,
                url=request.full_url,
                headers={"Content-Range": f"bytes 12-{len(whl_bytes) - 1}/{len(whl_bytes)}"},
            )

        mgr = QwenRuntimeManager(
            tmp_path / "retry",
            manifest,
            opener=flaky_opener,
            import_check=lambda _p: (True, ""),
        )
        assert mgr.install().state == "ready"
        assert seen_ranges == [None, "bytes=12-"]

        corrupt_mgr = QwenRuntimeManager(
            tmp_path / "corrupt",
            manifest,
            opener=lambda req, timeout=30: _FakeHttpResponse(
                b"z" * len(whl_bytes), url=req.full_url
            ),
            import_check=lambda _p: (True, ""),
        )
        failed = corrupt_mgr.install()
        assert failed.state == "failed"
        assert "checksum mismatch" in failed.error
        assert not corrupt_mgr._archive_path(wheel).exists()


class TestQwenModelManagerHardening:
    @staticmethod
    def _profile():
        import hashlib

        from vienetts_app.core.qwen_model_manifest import QwenModelFile, QwenModelProfile

        blobs = {
            "config.json": b'{"model_type": "qwen3_tts"}',
            "tokenizer_config.json": b'{"tokenizer": true}',
            "speech_tokenizer/model.safetensors": b"speech-tokenizer-weights-682mb-fixture",
        }
        profile = QwenModelProfile(
            key="base",
            repo="Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            revision="a" * 40,
            files=(
                QwenModelFile(
                    "config.json",
                    len(blobs["config.json"]),
                    hashlib.sha256(blobs["config.json"]).hexdigest(),
                ),
            ),
            shared=(
                QwenModelFile(
                    "tokenizer_config.json",
                    len(blobs["tokenizer_config.json"]),
                    hashlib.sha256(blobs["tokenizer_config.json"]).hexdigest(),
                ),
                QwenModelFile(
                    "speech_tokenizer/model.safetensors",
                    len(blobs["speech_tokenizer/model.safetensors"]),
                    hashlib.sha256(blobs["speech_tokenizer/model.safetensors"]).hexdigest(),
                ),
            ),
        )
        return profile, blobs

    def test_download_uses_staging_root_prunes_cache_nested_shared_and_writes_stamps(
        self, tmp_path, monkeypatch
    ) -> None:
        import json
        import os
        from pathlib import Path

        from vienetts_app.core import managed_install as mi
        from vienetts_app.core.qwen_model_manager import QwenModelManager

        profile, blobs = self._profile()
        local_dirs: dict[str, str] = {}

        def hf_like_downloader(
            *,
            repo_id: str,
            filename: str,
            revision: str,
            local_dir: str,
            local_dir_use_symlinks: bool = False,
        ) -> Path:
            local_dirs[filename] = local_dir
            root = Path(local_dir)
            out = root / filename
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(blobs[filename])
            cache_dir = root / ".cache" / "huggingface" / "download"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / f"{Path(filename).name}.metadata").write_text("etag", encoding="utf-8")
            return out

        hash_calls: list[Path] = []
        real_hash = mi.sha256_of
        monkeypatch.setattr(mi, "sha256_of", lambda p: hash_calls.append(p) or real_hash(p))

        manager = QwenModelManager(
            tmp_path / "models", "base", profile=profile, downloader=hf_like_downloader
        )
        status = manager.install()
        assert status.state == "ready", status.error

        # local_dir for speech_tokenizer/model.safetensors MUST be .staging/shared.
        assert Path(local_dirs["speech_tokenizer/model.safetensors"]) == (
            tmp_path / "models" / ".staging" / "shared"
        )
        # Zero double-nested speech_tokenizer/, zero .cache/, zero leftover .staging/shared/.
        assert not (
            tmp_path / "models" / "shared" / "speech_tokenizer" / "speech_tokenizer"
        ).exists()
        assert not (tmp_path / "models" / "base" / ".cache").exists()
        assert not (tmp_path / "models" / "shared" / ".cache").exists()
        assert not (tmp_path / "models" / ".staging" / "shared").exists()

        # Pre-promotion stamps mean exactly 1 hash per file during install, 0 in promote/inspect.
        assert len(hash_calls) == len(profile.files) + len(profile.shared)
        meta = json.loads((tmp_path / "models" / "base" / "install.json").read_text("utf-8"))
        assert set(meta["stamps"]) == {
            "files/config.json",
            "shared/tokenizer_config.json",
            "shared/speech_tokenizer/model.safetensors",
        }
        hash_calls.clear()
        assert manager.inspect().state == "ready"
        assert hash_calls == []

        # Proactively prune legacy double-nested speech_tokenizer/ on inspect & repair.
        for base_dir in (
            tmp_path / "models" / "shared",
            tmp_path / "models" / ".staging" / "shared",
        ):
            nested = base_dir / "speech_tokenizer" / "speech_tokenizer" / "model.safetensors"
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_bytes(b"682mb-duplicate")
        assert manager.inspect().state == "ready"
        assert not (
            tmp_path / "models" / "shared" / "speech_tokenizer" / "speech_tokenizer"
        ).exists()
        assert not (
            tmp_path / "models" / ".staging" / "shared" / "speech_tokenizer" / "speech_tokenizer"
        ).exists()

        nested_repair = (
            tmp_path / "models" / "shared" / "speech_tokenizer" / "speech_tokenizer" / "leak.bin"
        )
        nested_repair.parent.mkdir(parents=True, exist_ok=True)
        nested_repair.write_bytes(b"leak-on-repair")
        assert manager.repair().state == "ready"
        assert not nested_repair.parent.exists()

        # Strict remove fails honestly when locked.
        monkeypatch.setattr(mi, "REMOVAL_RETRY_SECONDS", 0.0)
        monkeypatch.setattr(mi.shutil, "rmtree", lambda *_a, **_k: None)
        removed = manager.remove()
        assert removed.state == "failed"
        assert "could not remove" in removed.error

        # Windows extended path passed to hf_hub_download.
        monkeypatch.undo()
        win_dirs: list[str] = []

        def win_downloader(**kwargs):
            win_dirs.append(str(kwargs["local_dir"]))
            dest = (
                tmp_path
                / "win"
                / ".staging"
                / ("base" if kwargs["filename"] == "config.json" else "shared")
                / kwargs["filename"]
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blobs[kwargs["filename"]])
            return dest

        win_mgr = QwenModelManager(
            tmp_path / "win", "base", profile=profile, downloader=win_downloader
        )
        monkeypatch.setattr(os, "name", "nt")
        orig_resolve = Path.resolve

        def fake_resolve(self, *args, **kwargs):
            if ".staging" in str(self):

                class ShortWinPath:
                    def __str__(self) -> str:
                        return r"C:\q\.staging\shared"

                    def is_relative_to(self, _other: object) -> bool:
                        return True

                return ShortWinPath()
            return orig_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", fake_resolve)
        assert win_mgr.install().state == "ready"
        assert len(win_dirs) == 3
        assert all(d.startswith("\\\\?\\C:\\") for d in win_dirs)

    def test_transient_retry_and_checksum_mismatch_clears_hf_metadata(
        self, tmp_path, monkeypatch
    ) -> None:
        import http.client
        from pathlib import Path

        from vienetts_app.core import qwen_model_manager as qmm_mod
        from vienetts_app.core.qwen_model_manager import QwenModelManager

        monkeypatch.setattr(qmm_mod, "_DOWNLOAD_BACKOFF_BASE_SECONDS", 0.0)
        profile, blobs = self._profile()
        attempts: dict[str, int] = {}

        def flaky_downloader(**kwargs) -> Path:
            name = kwargs["filename"]
            attempts[name] = attempts.get(name, 0) + 1
            out = Path(kwargs["local_dir"]) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            if name == "config.json" and attempts[name] == 1:
                out.write_bytes(blobs[name][:5])
                raise http.client.IncompleteRead(blobs[name][:5])
            out.write_bytes(blobs[name])
            return out

        mgr = QwenModelManager(
            tmp_path / "retry", "base", profile=profile, downloader=flaky_downloader
        )
        assert mgr.install().state == "ready"
        assert attempts["config.json"] == 2

        # Checksum mismatch unlinks corrupt file and clears HF .metadata.
        def corrupt_downloader(**kwargs) -> Path:
            root = Path(kwargs["local_dir"])
            out = root / kwargs["filename"]
            out.parent.mkdir(parents=True, exist_ok=True)
            dl_cache = root / ".cache" / "huggingface" / "download"
            dl_cache.mkdir(parents=True, exist_ok=True)
            (dl_cache / "config.json.metadata").write_text("stale", encoding="utf-8")
            out.write_bytes(b"corrupt")
            return out

        bad_mgr = QwenModelManager(
            tmp_path / "bad", "base", profile=profile, downloader=corrupt_downloader
        )
        failed = bad_mgr.install()
        assert failed.state == "failed"
        assert "checksum mismatch" in failed.error
        assert not (tmp_path / "bad" / ".staging" / "base" / "config.json").exists()
        dl_cache = tmp_path / "bad" / ".staging" / "base" / ".cache" / "huggingface" / "download"
        assert list(dl_cache.glob("*.metadata")) == []
