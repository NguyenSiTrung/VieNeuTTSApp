"""Managed Qwen3-TTS GGUF model installation (track qwen_gguf_engine_20260923).

Four talkers (base/customvoice × Q8_0/Q4_K_M) and two shared tokenizers are
pinned in ``qwen_gguf_model_manifests.json``; the manager installs one
``QwenVariant`` at a time, sharing each tokenizer across the profiles that
quantize to it. These tests pin the contract with tiny synthetic GGUF blobs —
real files are ~600 MB and never enter unit tests.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import struct
from pathlib import Path

import pytest

from vienetts_app.core import qwen_gguf_model_manifest as manifest_module
from vienetts_app.core.qwen_gguf_model_manifest import (
    QwenGgufFile,
    QwenGgufModelManifest,
    load_manifest_from_data,
    parse_gguf_metadata,
)
from vienetts_app.core.qwen_gguf_models import (
    QwenGgufModelManager,
    unreferenced_shared_files,
)
from vienetts_app.core.qwen_variants import variant_for

# --- synthetic GGUF fixtures -------------------------------------------------

_TALKER_ARCH = "qwen3-tts"
_TOKENIZER_ARCH = "qwen3-tts-tokenizer"


def _gguf_bytes(metadata: dict[str, object], *, blob: bytes = b"tensor-data") -> bytes:
    """A minimal but real GGUF: magic, v3 header, metadata KV section, blob."""
    out = io.BytesIO()
    out.write(b"GGUF")
    out.write(struct.pack("<I", 3))
    out.write(struct.pack("<Q", 1))  # n_tensors
    out.write(struct.pack("<Q", len(metadata)))
    for key, value in metadata.items():
        encoded = key.encode("utf-8")
        out.write(struct.pack("<Q", len(encoded)))
        out.write(encoded)
        if isinstance(value, str):
            out.write(struct.pack("<I", 8))  # STRING
            out.write(struct.pack("<Q", len(value.encode("utf-8"))))
            out.write(value.encode("utf-8"))
        elif isinstance(value, int):
            out.write(struct.pack("<I", 10))  # UINT64
            out.write(struct.pack("<Q", value))
        else:  # pragma: no cover - fixtures only use str/int
            raise AssertionError(f"unsupported fixture type: {type(value)}")
    out.write(blob)
    return out.getvalue()


def _talker_metadata(model_type: str, quantization: str) -> dict[str, object]:
    return {
        "general.architecture": _TALKER_ARCH,
        "general.name": f"Qwen3-TTS-12Hz-0.6B-{model_type}",
        "qwen3-tts.model_type": model_type,
        "qwen3-tts.tokenizer_type": "qwen3_tts_tokenizer_12hz",
        "general.file_type": quantization,
    }


def _tokenizer_metadata(quantization: str) -> dict[str, object]:
    return {
        "general.architecture": _TOKENIZER_ARCH,
        "general.name": "Qwen3-TTS-Tokenizer-12Hz",
        "general.file_type": quantization,
    }


REQUIRED_TALKER_KEYS = ("general.architecture", "qwen3-tts.model_type", "general.file_type")
REQUIRED_TOKENIZER_KEYS = ("general.architecture", "general.file_type")

TALKER_B8 = _gguf_bytes(_talker_metadata("base", "Q8_0"), blob=b"base-q8-talker")
TALKER_B4 = _gguf_bytes(_talker_metadata("base", "Q4_K_M"), blob=b"base-q4-talker")
TALKER_C8 = _gguf_bytes(_talker_metadata("custom_voice", "Q8_0"), blob=b"cv-q8-talker")
TALKER_C4 = _gguf_bytes(_talker_metadata("custom_voice", "Q4_K_M"), blob=b"cv-q4-talker")
TOK_Q8 = _gguf_bytes(_tokenizer_metadata("Q8_0"), blob=b"tok-q8")
TOK_Q4 = _gguf_bytes(_tokenizer_metadata("Q4_K_M"), blob=b"tok-q4")


def _file(path: str, content: bytes, metadata: dict[str, object]) -> QwenGgufFile:
    return QwenGgufFile(
        path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        metadata={k: metadata[k] for k in metadata},
    )


def mini_manifest() -> QwenGgufModelManifest:
    """A 2-profile × 2-quantization manifest over the synthetic blobs."""
    talkers = {}
    for profile, model_type, quant, blob in (
        ("base", "base", "Q8_0", TALKER_B8),
        ("base", "base", "Q4_K_M", TALKER_B4),
        ("customvoice", "custom_voice", "Q8_0", TALKER_C8),
        ("customvoice", "custom_voice", "Q4_K_M", TALKER_C4),
    ):
        key = f"{profile}-{quant}"
        talkers[key] = _file(
            f"qwen-talker-0.6b-{profile}-{quant}.gguf",
            blob,
            _talker_metadata(model_type, quant),
        )
    tokenizers = {
        "Q8_0": _file("qwen-tokenizer-12hz-Q8_0.gguf", TOK_Q8, _tokenizer_metadata("Q8_0")),
        "Q4_K_M": _file("qwen-tokenizer-12hz-Q4_K_M.gguf", TOK_Q4, _tokenizer_metadata("Q4_K_M")),
    }
    return QwenGgufModelManifest(
        format_version="qwen-gguf-model-v1",
        repo="Serveurperso/Qwen3-TTS-GGUF",
        revision="b7ee2e8c7459c3bea99da23e3d178125a7d1713c",
        talkers=talkers,
        tokenizers=tokenizers,
    )


def contents_for(manifest: QwenGgufModelManifest) -> dict[str, bytes]:
    blobs = {
        "qwen-talker-0.6b-base-Q8_0.gguf": TALKER_B8,
        "qwen-talker-0.6b-base-Q4_K_M.gguf": TALKER_B4,
        "qwen-talker-0.6b-customvoice-Q8_0.gguf": TALKER_C8,
        "qwen-talker-0.6b-customvoice-Q4_K_M.gguf": TALKER_C4,
        "qwen-tokenizer-12hz-Q8_0.gguf": TOK_Q8,
        "qwen-tokenizer-12hz-Q4_K_M.gguf": TOK_Q4,
    }
    return blobs


def file_downloader(payload: dict[str, bytes], calls: list[str] | None = None):
    """Serve pinned files by their URL path (the repo-relative filename)."""

    def download(url: str, target: Path) -> None:
        name = url.rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload[name])

    return download


_ENGINE_ID = {"base": "qwen_base_0_6b", "customvoice": "qwen_custom_0_6b"}


def _variant(profile: str, quant: str):
    return variant_for(_ENGINE_ID[profile], model_format="gguf", quantization=quant)


def _manager(
    root: Path,
    profile: str,
    quant: str,
    *,
    manifest: QwenGgufModelManifest | None = None,
    **kwargs,
) -> QwenGgufModelManager:
    return QwenGgufModelManager(
        root,
        _variant(profile, quant),
        manifest=manifest if manifest is not None else mini_manifest(),
        **kwargs,
    )


# --- install / status --------------------------------------------------------


def test_install_promotes_the_exact_variant_pair(tmp_path: Path) -> None:
    manifest = mini_manifest()
    manager = _manager(
        tmp_path,
        "base",
        "Q8_0",
        manifest=manifest,
        downloader=file_downloader(contents_for(manifest)),
    )

    status = manager.install()

    assert status.ready
    assert status.location is not None
    assert status.talker_path is not None and status.talker_path.name.endswith("base-Q8_0.gguf")
    assert status.tokenizer_path is not None and status.tokenizer_path.name.endswith(
        "12hz-Q8_0.gguf"
    )
    assert status.model_identity == manifest.recipe_for("base", "Q8_0").model_identity
    assert status.talker_path.is_relative_to(manager.root)
    assert status.tokenizer_path.is_relative_to(manager.root)


def test_both_profiles_in_one_quant_share_the_tokenizer(tmp_path: Path) -> None:
    manifest = mini_manifest()
    calls: list[str] = []
    downloader = file_downloader(contents_for(manifest), calls)
    base = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=downloader)
    custom = _manager(tmp_path, "customvoice", "Q8_0", manifest=manifest, downloader=downloader)

    assert base.install().ready
    calls.clear()
    assert custom.install().ready

    # The shared tokenizer was never re-downloaded for the second profile.
    assert "qwen-tokenizer-12hz-Q8_0.gguf" not in calls
    assert base.status().tokenizer_path == custom.status().tokenizer_path


def test_each_quantization_has_its_own_codec(tmp_path: Path) -> None:
    manifest = mini_manifest()
    downloader = file_downloader(contents_for(manifest))
    q8 = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=downloader)
    q4 = _manager(tmp_path, "base", "Q4_K_M", manifest=manifest, downloader=downloader)

    assert q8.install().ready
    assert q4.install().ready
    assert q8.status().tokenizer_path != q4.status().tokenizer_path
    assert (tmp_path / "shared" / "qwen-tokenizer-12hz-Q8_0.gguf").is_file()
    assert (tmp_path / "shared" / "qwen-tokenizer-12hz-Q4_K_M.gguf").is_file()


def test_status_never_depends_on_the_pytorch_runtime(tmp_path: Path) -> None:
    # The managed GGUF tree is standalone: no site-packages, no torch import.
    manager = _manager(tmp_path, "base", "Q8_0")
    status = manager.status()
    assert status.state == "unavailable"
    assert status.location is None


def test_removing_one_variant_keeps_the_shared_codec(tmp_path: Path) -> None:
    manifest = mini_manifest()
    downloader = file_downloader(contents_for(manifest))
    base = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=downloader)
    custom = _manager(tmp_path, "customvoice", "Q8_0", manifest=manifest, downloader=downloader)
    base.install()
    custom.install()

    assert base.remove().state == "unavailable"
    # customvoice still ready; the codec survives.
    assert custom.status().ready
    assert custom.status().tokenizer_path.is_file()


def test_drop_shared_removes_only_unreferenced_tokenizers(tmp_path: Path) -> None:
    manifest = mini_manifest()
    downloader = file_downloader(contents_for(manifest))
    base8 = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=downloader)
    custom8 = _manager(tmp_path, "customvoice", "Q8_0", manifest=manifest, downloader=downloader)
    base4 = _manager(tmp_path, "base", "Q4_K_M", manifest=manifest, downloader=downloader)
    base8.install()
    custom8.install()
    base4.install()

    # Remove both Q8_0 variants: their codec becomes unreferenced, Q4's stays.
    base8.remove(drop_shared=True)
    assert (tmp_path / "shared" / "qwen-tokenizer-12hz-Q8_0.gguf").is_file()
    custom8.remove(drop_shared=True)
    assert not (tmp_path / "shared" / "qwen-tokenizer-12hz-Q8_0.gguf").exists()
    assert (tmp_path / "shared" / "qwen-tokenizer-12hz-Q4_K_M.gguf").is_file()
    assert unreferenced_shared_files(tmp_path, manifest) == ()


def test_remove_refuses_while_in_use(tmp_path: Path) -> None:
    manifest = mini_manifest()
    manager = _manager(
        tmp_path,
        "base",
        "Q8_0",
        manifest=manifest,
        downloader=file_downloader(contents_for(manifest)),
    )
    manager.install()

    status = manager.remove(in_use=True)
    assert status.state == "failed"
    assert "in use" in status.error
    assert manager.status().ready


def test_a_source_clone_is_never_deleted(tmp_path: Path) -> None:
    """A user-supplied source tree is copied from, never consumed."""
    manifest = mini_manifest()
    source = tmp_path / "clone"
    for variant_dir, blob in (("base-Q8_0", TALKER_B8),):
        (source / variant_dir).mkdir(parents=True)
        (source / variant_dir / "qwen-talker-0.6b-base-Q8_0.gguf").write_bytes(blob)
    (source / "shared").mkdir(exist_ok=True)
    (source / "shared" / "qwen-tokenizer-12hz-Q8_0.gguf").write_bytes(TOK_Q8)

    manager = _manager(tmp_path / "models", "base", "Q8_0", manifest=manifest)
    assert manager.install_offline(source).ready
    manager.remove()
    # The source clone is untouched.
    assert (source / "base-Q8_0" / "qwen-talker-0.6b-base-Q8_0.gguf").is_file()
    assert (source / "shared" / "qwen-tokenizer-12hz-Q8_0.gguf").is_file()


def test_an_offline_pack_with_unexpected_paths_is_rejected(tmp_path: Path) -> None:
    manifest = mini_manifest()
    source = tmp_path / "clone"
    (source / "base-Q8_0").mkdir(parents=True)
    (source / "base-Q8_0" / "qwen-talker-0.6b-base-Q8_0.gguf").write_bytes(TALKER_B8)
    (source / "base-Q8_0" / "evil.bin").write_bytes(b"unexpected")
    (source / "shared").mkdir(exist_ok=True)
    (source / "shared" / "qwen-tokenizer-12hz-Q8_0.gguf").write_bytes(TOK_Q8)

    manager = _manager(tmp_path / "models", "base", "Q8_0", manifest=manifest)
    status = manager.install_offline(source)
    assert status.state == "failed"
    assert "unexpected" in status.error


def test_a_wrong_gguf_model_type_is_rejected(tmp_path: Path) -> None:
    # The manifest pins the real file's digest but declares the wrong expected
    # metadata — verification must fail on the metadata gate, not the digest.
    manifest = mini_manifest()
    recipe = manifest.recipe_for("base", "Q8_0")
    wrong = dataclasses.replace(
        recipe.talker,
        metadata={**recipe.talker.metadata, "qwen3-tts.model_type": "custom_voice"},
    )
    manifest = dataclasses.replace(manifest, talkers={**manifest.talkers, "base-Q8_0": wrong})
    manager = _manager(
        tmp_path,
        "base",
        "Q8_0",
        manifest=manifest,
        downloader=file_downloader(contents_for(mini_manifest())),
    )
    status = manager.install()
    assert status.state == "failed"
    assert "model_type" in status.error or "metadata" in status.error


def test_a_checksum_mismatch_never_promotes(tmp_path: Path) -> None:
    manifest = mini_manifest()
    payload = contents_for(manifest)
    payload["qwen-talker-0.6b-base-Q8_0.gguf"] = b"not the pinned bytes"
    manager = _manager(
        tmp_path,
        "base",
        "Q8_0",
        manifest=manifest,
        downloader=file_downloader(payload),
    )

    status = manager.install()
    assert status.state == "failed"
    assert "checksum" in status.error or "does not match" in status.error
    assert not (tmp_path / "base-Q8_0" / "install.json").exists()


def test_cancellation_keeps_partials_and_resumes(tmp_path: Path) -> None:
    manifest = mini_manifest()
    calls: list[str] = []

    contents = contents_for(manifest)

    def download(url: str, target: Path) -> None:
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if len(calls) >= 2:
            target.write_bytes(contents[name][:10])
            from vienetts_app.core.managed_install import DownloadCancelled

            raise DownloadCancelled
        target.write_bytes(contents[name])

    manager = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=download)
    status = manager.install()
    assert status.state == "unavailable"
    assert not (tmp_path / "base-Q8_0" / "install.json").exists()

    calls.clear()
    manager = _manager(
        tmp_path,
        "base",
        "Q8_0",
        manifest=manifest,
        downloader=file_downloader(contents, calls),
    )
    assert manager.install().ready
    # The verified staged talker was kept — only the tokenizer downloaded again.
    assert calls == ["qwen-tokenizer-12hz-Q8_0.gguf"]


def test_insufficient_space_refuses_before_downloading(tmp_path: Path) -> None:
    manifest = mini_manifest()
    calls: list[str] = []

    class Usage:
        free = 1

    manager = _manager(
        tmp_path,
        "base",
        "Q8_0",
        manifest=manifest,
        disk_usage=lambda _p: Usage(),
        downloader=file_downloader(contents_for(manifest), calls),
    )
    status = manager.install()
    assert status.state == "failed"
    assert "insufficient disk space" in status.error
    assert calls == []


def test_a_promotion_failure_restores_the_previous_install(tmp_path: Path, monkeypatch) -> None:
    manifest = mini_manifest()
    downloader = file_downloader(contents_for(manifest))
    manager = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=downloader)
    assert manager.install().ready
    talker = manager.status().talker_path.read_bytes()

    # Damage only the shared codec — the variant's own files stay good — then
    # fail the staging→active swap mid-repair; the last-good install returns.
    manager.status().tokenizer_path.unlink()
    assert manager.status().state == "failed"

    real_replace = os.replace

    def fail_swap(src, dst):
        if f"{os.sep}.staging{os.sep}base-Q8_0" in str(src):
            raise OSError("simulated swap failure")
        real_replace(src, dst)

    monkeypatch.setattr("vienetts_app.core.qwen_gguf_models.os.replace", fail_swap)
    status = manager.repair()
    assert status.state == "failed"
    assert "swap failure" in status.error or "promotion failed" in status.error

    monkeypatch.undo()
    current = manager.status()
    assert current.ready  # talker and codec both verify again
    assert current.talker_path.read_bytes() == talker


def test_repair_after_a_failed_download_recovers(tmp_path: Path) -> None:
    manifest = mini_manifest()
    downloader = file_downloader(contents_for(manifest))
    manager = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=downloader)
    assert manager.install().ready
    talker = manager.status().talker_path.read_bytes()

    manager.status().talker_path.write_bytes(b"tampered")
    bad = contents_for(manifest)
    bad["qwen-talker-0.6b-base-Q8_0.gguf"] = b"corrupt"
    bad_manager = _manager(
        tmp_path, "base", "Q8_0", manifest=manifest, downloader=file_downloader(bad)
    )
    assert bad_manager.repair().state == "failed"

    assert manager.repair().ready
    assert manager.status().talker_path.read_bytes() == talker


def test_repair_reuses_verified_staged_and_shared_files(tmp_path: Path) -> None:
    manifest = mini_manifest()
    calls: list[str] = []
    downloader = file_downloader(contents_for(manifest), calls)
    manager = _manager(tmp_path, "base", "Q8_0", manifest=manifest, downloader=downloader)
    assert manager.install().ready

    manager.status().talker_path.write_bytes(b"tampered")
    calls.clear()
    assert manager.repair().ready
    # Only the talker was re-fetched; the shared codec verifies in place.
    assert calls == ["qwen-talker-0.6b-base-Q8_0.gguf"]


def test_an_official_variant_cannot_be_installed_here(tmp_path: Path) -> None:
    official = variant_for("qwen_base_0_6b", model_format="official")
    with pytest.raises(Exception, match="gguf"):
        QwenGgufModelManager(tmp_path, official, manifest=mini_manifest())


# --- manifest / parser --------------------------------------------------------


def test_parse_gguf_metadata_reads_only_the_header() -> None:
    blob = _gguf_bytes({"general.architecture": "qwen3-tts", "x.n": 42}, blob=b"BLOB")
    read = io.BytesIO(blob).read
    meta = parse_gguf_metadata(read)
    assert meta["general.architecture"] == "qwen3-tts"
    assert meta["x.n"] == 42


def test_parse_gguf_metadata_rejects_non_gguf() -> None:
    with pytest.raises(ValueError, match="GGUF"):
        parse_gguf_metadata(io.BytesIO(b"NOPE" + b"0" * 64).read)


def test_the_shipped_manifest_covers_all_four_variants() -> None:
    manifest = manifest_module.MANIFEST
    assert manifest is not None
    for profile in ("base", "customvoice"):
        for quant in ("Q8_0", "Q4_K_M"):
            recipe = manifest.recipe_for(profile, quant)
            assert recipe is not None
            assert recipe.talker.metadata["general.architecture"] == "qwen3-tts"
            assert recipe.talker.metadata["general.file_type"] == quant
            assert recipe.tokenizer.metadata["general.architecture"] == ("qwen3-tts-tokenizer")
    # Shared codec: both profiles in a quant pin the same tokenizer file.
    q8 = {p: manifest.recipe_for(p, "Q8_0") for p in ("base", "customvoice")}
    assert q8["base"].tokenizer == q8["customvoice"].tokenizer


def test_shipped_manifest_pins_match_the_requirements() -> None:
    requirements = json.loads(
        Path("packaging/qwen-gguf-runtime-requirements.json").read_text(encoding="utf-8")
    )
    manifest = manifest_module.MANIFEST
    assert manifest is not None
    assert manifest.repo == requirements["upstream"]["models"]["repo"]
    assert manifest.revision == requirements["upstream"]["models"]["revision"]
    for entry in requirements["variants"]:
        recipe = manifest.recipe_for(entry["profile"], entry["quantization"])
        assert recipe.talker.path == entry["talker"]["file"]
        assert recipe.talker.sha256 == entry["talker"]["sha256"]
        assert recipe.talker.size_bytes == entry["talker"]["size"]
        assert recipe.tokenizer.path == entry["tokenizer"]["file"]
        assert recipe.tokenizer.sha256 == entry["tokenizer"]["sha256"]


def test_load_manifest_rejects_a_wrong_revision() -> None:
    data = _manifest_data(mini_manifest())
    data["revision"] = "deadbeef"
    with pytest.raises(ValueError, match="revision"):
        load_manifest_from_data(data)


def test_load_manifest_rejects_wrong_metadata_expectations() -> None:
    data = _manifest_data(mini_manifest())
    data["talkers"]["base-Q8_0"]["metadata"]["general.architecture"] = "llama"
    with pytest.raises(ValueError, match="architecture"):
        load_manifest_from_data(data)


def test_load_manifest_rejects_a_missing_variant() -> None:
    data = _manifest_data(mini_manifest())
    del data["talkers"]["base-Q4_K_M"]
    with pytest.raises(ValueError, match="base-Q4_K_M|missing"):
        load_manifest_from_data(data)


def _manifest_data(manifest: QwenGgufModelManifest) -> dict:
    return {
        "formatVersion": manifest.format_version,
        "repo": manifest.repo,
        "revision": manifest.revision,
        "talkers": {
            key: {
                "path": f.path,
                "sizeBytes": f.size_bytes,
                "sha256": f.sha256,
                "metadata": dict(f.metadata),
            }
            for key, f in manifest.talkers.items()
        },
        "tokenizers": {
            key: {
                "path": f.path,
                "sizeBytes": f.size_bytes,
                "sha256": f.sha256,
                "metadata": dict(f.metadata),
            }
            for key, f in manifest.tokenizers.items()
        },
    }
