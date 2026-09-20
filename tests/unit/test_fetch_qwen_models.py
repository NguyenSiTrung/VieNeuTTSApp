"""Tests for the maintainer-only Qwen model lock generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vienetts_app.core import qwen_model_manifest as qm

SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "fetch_qwen_models.py"


def load_lock_script():
    spec = importlib.util.spec_from_file_location("fetch_qwen_models", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


LISTING = {
    "siblings": [
        {"rfilename": ".gitattributes", "size": 20},
        {"rfilename": "config.json", "size": 8},
        {"rfilename": "model.safetensors", "size": 4096, "lfs": {"sha256": "a" * 64}},
        {
            "rfilename": "speech_tokenizer/model.safetensors",
            "size": 2048,
            "lfs": {"sha256": "b" * 64},
        },
    ]
}
CONTENT = {".gitattributes": b"*.safetensors filter=lfs", "config.json": b'{"a": 1}'}
LISTING["siblings"][0]["size"] = len(CONTENT[".gitattributes"])
LISTING["siblings"][1]["size"] = len(CONTENT["config.json"])

REQUIREMENTS = {
    "modelPins": {
        "customvoice": {"repo": "Qwen/customvoice", "revision": "c" * 40},
        "base": {"repo": "Qwen/base", "revision": "b" * 40},
    }
}


def listing_for(repo: str) -> dict:
    payload = json.loads(json.dumps(LISTING))
    if repo == "Qwen/base":
        payload["siblings"][2]["lfs"]["sha256"] = "d" * 64
    return payload


def fetch_json(url: str) -> object:
    return listing_for("Qwen/base" if "/Qwen/base/" in url else "Qwen/customvoice")


def fetch_bytes(url: str) -> bytes:
    return CONTENT[url.rsplit("/", 1)[-1]]


def test_repository_listing_pins_lfs_digests_and_rejects_bad_entries() -> None:
    lock = load_lock_script()
    records = lock.parse_repo_listing(LISTING)

    assert records[2] is not None
    assert records[2].sha256 == "a" * 64
    assert records[1] is None  # no digest in the listing: hash it directly

    with pytest.raises(lock.LockError, match="unsafe model path"):
        lock.parse_repo_listing({"siblings": [{"rfilename": "../escape", "size": 1}]})
    with pytest.raises(lock.LockError, match="no usable size"):
        lock.parse_repo_listing({"siblings": [{"rfilename": "a.bin", "size": 0}]})
    with pytest.raises(lock.LockError, match="duplicate"):
        lock.parse_repo_listing(
            {"siblings": [{"rfilename": "a.bin", "size": 1}, {"rfilename": "a.bin", "size": 1}]}
        )


def test_resolve_listing_hashes_files_without_lfs_metadata() -> None:
    lock = load_lock_script()
    records = lock.resolve_listing(
        "Qwen/customvoice", "c" * 40, fetch_json=fetch_json, fetch_bytes=fetch_bytes
    )
    by_path = {record.path: record for record in records}

    assert by_path["config.json"].sha256 == sha(CONTENT["config.json"])
    assert by_path["model.safetensors"].sha256 == "a" * 64


def test_split_shared_requires_identical_path_size_and_digest() -> None:
    lock = load_lock_script()
    customvoice = lock.resolve_listing(
        "Qwen/customvoice", "c" * 40, fetch_json=fetch_json, fetch_bytes=fetch_bytes
    )
    base = lock.resolve_listing(
        "Qwen/base", "b" * 40, fetch_json=fetch_json, fetch_bytes=fetch_bytes
    )

    customvoice_only, base_only, shared = lock.split_shared(customvoice, base)

    assert {record.path for record in shared} == {
        ".gitattributes",
        "config.json",
        "speech_tokenizer/model.safetensors",
    }
    assert {record.path for record in customvoice_only} == {"model.safetensors"}
    assert {record.path for record in base_only} == {"model.safetensors"}


def test_render_manifest_produces_data_the_app_accepts() -> None:
    lock = load_lock_script()
    rendered = lock.render_manifest(REQUIREMENTS, fetch_json=fetch_json, fetch_bytes=fetch_bytes)

    manifest = qm.load_manifest_from_data(rendered)
    assert set(manifest.profiles) == {"customvoice", "base"}
    profile = manifest.profile_for("customvoice")
    assert profile is not None
    assert profile.repo == "Qwen/customvoice"
    assert [item.path for item in profile.files] == ["model.safetensors"]
    assert [item.path for item in profile.shared] == [
        "config.json",
        "speech_tokenizer/model.safetensors",
    ]
    assert profile.excluded == ()
    assert [entry["path"] for entry in rendered["shared"]["excluded"]] == [".gitattributes"]


def test_render_manifest_requires_shared_content() -> None:
    lock = load_lock_script()

    def disjoint_json(url: str) -> object:
        payload = listing_for("Qwen/customvoice")
        if "/Qwen/base/" in url:
            for index, sibling in enumerate(payload["siblings"]):
                sibling["rfilename"] = f"base-{index}/{sibling['rfilename']}"
        return payload

    with pytest.raises(lock.LockError, match="share no verified"):
        lock.render_manifest(REQUIREMENTS, fetch_json=disjoint_json, fetch_bytes=fetch_bytes)


def test_check_mode_detects_a_stale_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = load_lock_script()
    requirements = tmp_path / "requirements.json"
    requirements.write_text(json.dumps(REQUIREMENTS), encoding="utf-8")
    output = tmp_path / "models.json"
    output.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(lock, "render_manifest", lambda *_args, **_kwargs: {"stale": False})

    argv = ["--requirements", str(requirements), "--output", str(output), "--check"]
    assert lock.main(argv) == 1
    assert output.read_text(encoding="utf-8") == "stale"
