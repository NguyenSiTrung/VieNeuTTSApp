"""Tests for the maintainer-only Qwen3-TTS GGUF model lock generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vienetts_app.core import qwen_gguf_model_manifest as qm

SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "fetch_qwen_gguf_models.py"

REPO = "Serveurperso/Qwen3-TTS-GGUF"
REVISION = "b7ee2e8c7459c3bea99da23e3d178125a7d1713c"

FILES = {
    "qwen-talker-0.6b-base-Q8_0.gguf": (1001, "a1"),
    "qwen-talker-0.6b-base-Q4_K_M.gguf": (1002, "a2"),
    "qwen-talker-0.6b-customvoice-Q8_0.gguf": (1003, "a3"),
    "qwen-talker-0.6b-customvoice-Q4_K_M.gguf": (1004, "a4"),
    "qwen-tokenizer-12hz-Q8_0.gguf": (1005, "a5"),
    "qwen-tokenizer-12hz-Q4_K_M.gguf": (1006, "a6"),
}


def _sibling(path: str, size: int, seed: str, *, lfs: bool = True) -> dict:
    entry = {
        "rfilename": path,
        "blobId": hashlib.sha1(f"blob-{seed}".encode()).hexdigest(),
        "size": size,
    }
    if lfs:
        entry["lfs"] = {"sha256": hashlib.sha256(f"bytes-{seed}".encode()).hexdigest()}
    return entry


def _listing() -> dict:
    siblings = [_sibling(".gitattributes", 120, "git", lfs=False)]
    siblings += [_sibling(path, size, seed) for path, (size, seed) in FILES.items()]
    return {"siblings": siblings}


def _pin(path: str, seed: str) -> dict:
    size = FILES[path][0]
    sibling = _sibling(path, size, seed)
    return {
        "file": path,
        "size": size,
        "sha256": sibling["lfs"]["sha256"],
        "blobId": sibling["blobId"],
    }


def _requirements() -> dict:
    variants = []
    for profile, model_type in (("base", "base"), ("customvoice", "custom_voice")):
        for quant in ("Q8_0", "Q4_K_M"):
            talker = f"qwen-talker-0.6b-{profile}-{quant}.gguf"
            tokenizer = f"qwen-tokenizer-12hz-{quant}.gguf"
            variants.append(
                {
                    "key": f"{profile}-{quant}",
                    "profile": profile,
                    "quantization": quant,
                    "expectedModelType": model_type,
                    "talker": _pin(talker, FILES[talker][1]),
                    "tokenizer": _pin(tokenizer, FILES[tokenizer][1]),
                }
            )
    return {
        "upstream": {"models": {"repo": REPO, "revision": REVISION}},
        "variants": variants,
    }


def load_lock_script():
    spec = importlib.util.spec_from_file_location("fetch_qwen_gguf_models", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fetch_json(url: str) -> object:
    assert f"/api/models/{REPO}/revision/{REVISION}" in url
    return _listing()


def test_listing_indexes_lfs_files_by_path() -> None:
    lock = load_lock_script()
    records = lock.parse_repo_listing(_listing())
    talker = records["qwen-talker-0.6b-base-Q8_0.gguf"]
    assert talker.size_bytes == 1001
    assert len(talker.sha256) == 64
    assert len(talker.blob_id) == 40
    # Non-LFS bookkeeping is listed but carries no digest; pins reject it later.
    assert records[".gitattributes"].sha256 == ""

    with pytest.raises(lock.LockError, match="unsafe model path"):
        lock.parse_repo_listing({"siblings": [{"rfilename": "../escape", "size": 1}]})
    with pytest.raises(lock.LockError, match="duplicate"):
        lock.parse_repo_listing(
            {
                "siblings": [
                    {"rfilename": "a.gguf", "blobId": "x", "size": 1},
                    {"rfilename": "a.gguf", "blobId": "y", "size": 1},
                ]
            }
        )
    with pytest.raises(lock.LockError, match="no usable size"):
        lock.parse_repo_listing({"siblings": [{"rfilename": "a.gguf", "blobId": "x", "size": 0}]})


def test_render_manifest_locks_all_six_files_and_the_app_accepts_it() -> None:
    lock = load_lock_script()
    rendered = lock.render_manifest(_requirements(), fetch_json=fetch_json)

    manifest = qm.load_manifest_from_data(rendered)
    assert manifest.repo == REPO
    assert manifest.revision == REVISION
    assert set(manifest.talkers) == {
        "base-Q8_0",
        "base-Q4_K_M",
        "customvoice-Q8_0",
        "customvoice-Q4_K_M",
    }
    assert set(manifest.tokenizers) == {"Q8_0", "Q4_K_M"}

    recipe = manifest.recipe_for("customvoice", "Q4_K_M")
    assert recipe is not None
    assert recipe.talker.path == "qwen-talker-0.6b-customvoice-Q4_K_M.gguf"
    assert recipe.talker.metadata["qwen3-tts.model_type"] == "custom_voice"
    assert recipe.talker.metadata["general.file_type"] == "Q4_K_M"
    assert recipe.tokenizer.path == "qwen-tokenizer-12hz-Q4_K_M.gguf"
    assert recipe.tokenizer.metadata["general.architecture"] == "qwen3-tts-tokenizer"

    # One codec serves both profiles in the same quantization.
    assert (
        manifest.recipe_for("base", "Q8_0").tokenizer
        == manifest.recipe_for("customvoice", "Q8_0").tokenizer
    )


def test_render_manifest_rejects_size_and_digest_drift() -> None:
    lock = load_lock_script()

    def drifted_size(url: str) -> object:
        payload = _listing()
        for sibling in payload["siblings"]:
            if sibling["rfilename"] == "qwen-talker-0.6b-base-Q8_0.gguf":
                sibling["size"] += 1
        return payload

    with pytest.raises(lock.LockError, match="size drifted"):
        lock.render_manifest(_requirements(), fetch_json=drifted_size)

    def drifted_sha(url: str) -> object:
        payload = _listing()
        for sibling in payload["siblings"]:
            if sibling["rfilename"] == "qwen-tokenizer-12hz-Q8_0.gguf":
                sibling["lfs"]["sha256"] = "f" * 64
        return payload

    with pytest.raises(lock.LockError, match="SHA-256 drifted"):
        lock.render_manifest(_requirements(), fetch_json=drifted_sha)

    def drifted_blob(url: str) -> object:
        payload = _listing()
        for sibling in payload["siblings"]:
            if sibling["rfilename"] == "qwen-talker-0.6b-base-Q4_K_M.gguf":
                sibling["blobId"] = "0" * 40
        return payload

    with pytest.raises(lock.LockError, match="blob id drifted"):
        lock.render_manifest(_requirements(), fetch_json=drifted_blob)


def test_render_manifest_rejects_a_missing_pinned_file() -> None:
    lock = load_lock_script()

    def short_listing(url: str) -> object:
        payload = _listing()
        payload["siblings"] = [
            s
            for s in payload["siblings"]
            if s["rfilename"] != "qwen-talker-0.6b-customvoice-Q8_0.gguf"
        ]
        return payload

    with pytest.raises(lock.LockError, match="not in the repository listing"):
        lock.render_manifest(_requirements(), fetch_json=short_listing)


def test_render_manifest_rejects_incomplete_or_conflicting_variant_pins() -> None:
    lock = load_lock_script()

    requirements = _requirements()
    requirements["variants"] = [v for v in requirements["variants"] if v["key"] != "base-Q4_K_M"]
    with pytest.raises(lock.LockError, match="missing variant"):
        lock.render_manifest(requirements, fetch_json=fetch_json)

    requirements = _requirements()
    for variant in requirements["variants"]:
        if variant["key"] == "customvoice-Q8_0":
            variant["tokenizer"] = _pin(
                "qwen-tokenizer-12hz-Q4_K_M.gguf", FILES["qwen-tokenizer-12hz-Q4_K_M.gguf"][1]
            )
    with pytest.raises(lock.LockError, match="disagree on the Q8_0 codec"):
        lock.render_manifest(requirements, fetch_json=fetch_json)

    requirements = _requirements()
    requirements["variants"][0]["expectedModelType"] = "voice_design"
    with pytest.raises(lock.LockError, match="model_type"):
        lock.render_manifest(requirements, fetch_json=fetch_json)


def test_render_manifest_requires_an_immutable_revision() -> None:
    lock = load_lock_script()
    requirements = _requirements()
    requirements["upstream"]["models"]["revision"] = "main"
    with pytest.raises(lock.LockError, match="revision"):
        lock.render_manifest(requirements, fetch_json=fetch_json)


def test_check_mode_detects_a_stale_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = load_lock_script()
    requirements = tmp_path / "requirements.json"
    requirements.write_text(json.dumps(_requirements()), encoding="utf-8")
    output = tmp_path / "manifest.json"
    output.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(lock, "render_manifest", lambda *_args, **_kwargs: {"stale": False})

    argv = ["--requirements", str(requirements), "--output", str(output), "--check"]
    assert lock.main(argv) == 1


def test_check_mode_accepts_the_shipped_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = load_lock_script()
    requirements = tmp_path / "requirements.json"
    requirements.write_text(json.dumps(_requirements()), encoding="utf-8")
    output = tmp_path / "manifest.json"
    rendered = lock.render_manifest(_requirements(), fetch_json=fetch_json)
    output.write_text(json.dumps(rendered, indent=2, sort_keys=True) + "\n")
    monkeypatch.setattr(lock, "_fetch_json", fetch_json)

    argv = ["--requirements", str(requirements), "--output", str(output), "--check"]
    assert lock.main(argv) == 0
