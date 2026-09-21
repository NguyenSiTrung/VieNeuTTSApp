"""Qwen model installer lifecycle: profiles, shared files, offline packs."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from vienetts_app.core.qwen_model_manager import (
    QwenModelManager,
    installed_profiles,
    shared_is_used,
)
from vienetts_app.core.qwen_model_manifest import (
    FORMAT_VERSION,
    QwenModelFile,
    QwenModelManifest,
    QwenModelProfile,
)

SHARED_CONTENT = b"shared tokenizer payload"
WEIGHTS = b"profile weights"


def profile_for_test(key: str, *, own: bytes = WEIGHTS, revision: str = "a" * 40):
    shared = (
        QwenModelFile(
            "vocab.json", len(SHARED_CONTENT), hashlib.sha256(SHARED_CONTENT).hexdigest()
        ),
    )
    files = (
        QwenModelFile("model.safetensors", len(own), hashlib.sha256(own).hexdigest()),
        QwenModelFile("config.json", 3, hashlib.sha256(b"cfg").hexdigest()),
    )
    return QwenModelProfile(
        key=key,
        repo=f"Qwen/{key}",
        revision=revision,
        files=files,
        shared=shared,
    )


def manifest_for_test(*profiles: QwenModelProfile) -> QwenModelManifest:
    return QwenModelManifest(
        format_version=FORMAT_VERSION,
        shared_repo="Qwen/shared",
        shared_revision="f" * 40,
        profiles={profile.key: profile for profile in profiles},
    )


def payload(profile: QwenModelProfile) -> dict[str, bytes]:
    content = {record.path: WEIGHTS for record in profile.files}
    content["config.json"] = b"cfg"
    return content


def downloader_for(
    profiles: list[QwenModelProfile],
    calls: list[tuple[str, str]] | None = None,
    own: dict[str, bytes] | None = None,
):
    content: dict[tuple[str, str], bytes] = {}
    for profile in profiles:
        weights = (own or {}).get(profile.key, WEIGHTS)
        for record in profile.files:
            content[(profile.repo, record.path)] = (
                b"cfg" if record.path == "config.json" else weights
            )
        for record in profile.shared:
            content[(profile.repo, record.path)] = SHARED_CONTENT

    def download(*, repo_id: str, filename: str, revision: str, local_dir: str, **_kwargs) -> Path:
        if calls is not None:
            calls.append((repo_id, filename))
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content[(repo_id, filename)])
        return target

    return download


def test_install_promotes_a_verified_profile_with_shared_files(tmp_path: Path) -> None:
    profile = profile_for_test("customvoice")
    calls: list[tuple[str, str]] = []
    manager = QwenModelManager(
        tmp_path, "customvoice", profile, downloader=downloader_for([profile], calls)
    )

    status = manager.install()

    assert status.state == "ready"
    assert status.location is not None
    assert (tmp_path / "customvoice" / "model.safetensors").read_bytes() == WEIGHTS
    assert (tmp_path / "shared" / "vocab.json").read_bytes() == SHARED_CONTENT
    assert (tmp_path / "customvoice" / "install.json").is_file()
    assert sorted(calls) == [
        ("Qwen/customvoice", "config.json"),
        ("Qwen/customvoice", "model.safetensors"),
        ("Qwen/customvoice", "vocab.json"),
    ]
    assert not (tmp_path / ".staging" / "customvoice").exists()
    assert status.progress == 1.0


def test_profiles_install_independently_and_reuse_shared_files(tmp_path: Path) -> None:
    customvoice = profile_for_test("customvoice")
    base = profile_for_test("base", own=b"other weights")
    calls: list[tuple[str, str]] = []
    downloader = downloader_for([customvoice, base], calls, own={"base": b"other weights"})

    assert (
        QwenModelManager(tmp_path, "customvoice", customvoice, downloader=downloader)
        .install()
        .state
        == "ready"
    )
    assert (
        QwenModelManager(tmp_path, "base", base, downloader=downloader).install().state == "ready"
    )

    manifest = manifest_for_test(customvoice, base)
    assert calls.count(("Qwen/base", "vocab.json")) == 0  # shared file reused, not re-fetched
    assert installed_profiles(tmp_path, manifest) == ("customvoice", "base")

    removed = QwenModelManager(tmp_path, "customvoice", customvoice).remove()

    assert removed.state == "unavailable"
    assert not (tmp_path / "customvoice").exists()
    assert (tmp_path / "shared" / "vocab.json").is_file()
    assert QwenModelManager(tmp_path, "base", base).inspect().state == "ready"
    assert shared_is_used(tmp_path, manifest) is True

    QwenModelManager(tmp_path, "base", base).remove(remove_shared=True)
    assert not (tmp_path / "shared").exists()
    assert shared_is_used(tmp_path, manifest) is False


def test_checksum_failure_never_promotes_and_reports_the_file(tmp_path: Path) -> None:
    profile = profile_for_test("customvoice")

    def bad_download(*, repo_id: str, filename: str, local_dir: str, **_kwargs) -> Path:
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"tampered")
        return target

    manager = QwenModelManager(tmp_path, "customvoice", profile, downloader=bad_download)

    status = manager.install()

    assert status.state == "failed"
    assert "checksum mismatch" in status.error
    assert status.location is None
    assert not (tmp_path / "customvoice").exists()
    assert not list(tmp_path.glob("*/install.json"))
    assert not (tmp_path / "shared" / "vocab.json").exists()


def test_manifest_drift_and_corruption_are_reported(tmp_path: Path) -> None:
    profile = profile_for_test("customvoice")
    manager = QwenModelManager(
        tmp_path, "customvoice", profile, downloader=downloader_for([profile])
    )
    assert manager.install().state == "ready"

    (tmp_path / "shared" / "vocab.json").write_bytes(b"corrupt")
    assert manager.inspect().state == "failed"
    assert "incomplete" in manager.inspect().error

    assert manager.repair().state == "ready"
    assert manager.inspect().state == "ready"

    (tmp_path / "customvoice" / "install.json").write_text(
        json.dumps({"format": "qwen-model-v1", "profile": "customvoice"}), encoding="utf-8"
    )
    drifted = manager.inspect()

    assert drifted.state == "failed"
    assert "does not match the manifest" in drifted.error


def test_repair_keeps_valid_staged_files(tmp_path: Path) -> None:
    profile = profile_for_test("customvoice")
    calls: list[tuple[str, str]] = []
    manager = QwenModelManager(
        tmp_path, "customvoice", profile, downloader=downloader_for([profile], calls)
    )
    staged = tmp_path / ".staging" / "customvoice" / "model.safetensors"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(WEIGHTS)

    status = manager.repair()

    assert status.state == "ready"
    assert ("Qwen/customvoice", "model.safetensors") not in calls


def test_install_reports_in_flight_bytes_while_a_large_file_downloads(
    tmp_path: Path,
) -> None:
    """Byte-level progress: the bar must move INSIDE a single file download.

    hf_hub_download owns the calling thread for a whole file, so progress is
    measured off the staging tree — without it the ~2 GB checkpoints sit at
    0% for the entire download and then jump to the file boundary.
    """
    profile = profile_for_test("customvoice")
    whole_files = {0, len(WEIGHTS), len(WEIGHTS) + 3, len(WEIGHTS) + 3 + len(SHARED_CONTENT)}
    content_map = {
        "model.safetensors": WEIGHTS,
        "config.json": b"cfg",
        "vocab.json": SHARED_CONTENT,
    }

    def gradual_download(*, repo_id: str, filename: str, local_dir: str, **_kwargs) -> Path:
        content = content_map[filename]
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            for index in range(0, len(content), 4):
                handle.write(content[index : index + 4])
                handle.flush()
                time.sleep(0.01)
        return target

    statuses: list = []
    manager = QwenModelManager(
        tmp_path,
        "customvoice",
        profile,
        downloader=gradual_download,
        progress_interval_seconds=0.005,
    )

    status = manager.install(on_progress=statuses.append)

    assert status.state == "ready"
    downloaded = [s.installed_bytes for s in statuses if s.state == "downloading"]
    # Whole-file boundaries alone cannot produce this many distinct values:
    # in-flight samples between boundaries prove the bar moved continuously.
    assert len(set(downloaded)) > len(profile.files) + len(profile.shared)
    assert all(0 <= value <= status.required_bytes for value in downloaded)
    assert any(value not in whole_files for value in downloaded)


def test_low_disk_space_refuses_before_downloading(tmp_path: Path) -> None:
    profile = profile_for_test("customvoice")
    calls: list[tuple[str, str]] = []
    manager = QwenModelManager(
        tmp_path,
        "customvoice",
        profile,
        downloader=downloader_for([profile], calls),
        disk_usage=lambda _path: type("Usage", (), {"free": 1})(),
    )

    status = manager.install()

    assert status.state == "failed"
    assert "insufficient disk space" in status.error
    assert calls == []


def test_remove_refuses_in_use_and_cancel_staging_keeps_the_profile(
    tmp_path: Path,
) -> None:
    profile = profile_for_test("customvoice")
    manager = QwenModelManager(
        tmp_path, "customvoice", profile, downloader=downloader_for([profile])
    )
    assert manager.install().state == "ready"
    staged = tmp_path / ".staging" / "customvoice" / "partial.bin"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"partial")

    refused = manager.remove(in_use=True)
    manager.cancel_staging()

    assert refused.state == "failed"
    assert "in use" in refused.error
    assert manager.inspect().state == "ready"
    assert not staged.exists()


def test_failed_promotion_restores_the_previous_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = profile_for_test("customvoice")
    manager = QwenModelManager(
        tmp_path, "customvoice", profile, downloader=downloader_for([profile])
    )
    assert manager.install().state == "ready"
    staging = tmp_path / ".staging" / "customvoice"
    (staging / "extra").mkdir(parents=True)
    (staging / "install.json").write_text(json.dumps(manager._metadata()), encoding="utf-8")
    real_replace = __import__("os").replace

    def fail_promotion(source, target):
        if Path(source) == staging and Path(target) == tmp_path / "customvoice":
            raise OSError("simulated promotion failure")
        return real_replace(source, target)

    monkeypatch.setattr("vienetts_app.core.qwen_model_manager.os.replace", fail_promotion)

    status = manager._promote_staging()

    assert status.state == "failed"
    assert manager.inspect().state == "ready"
    assert (tmp_path / "customvoice").is_dir()


def test_offline_pack_install_verifies_layout_and_content(tmp_path: Path) -> None:
    profile = profile_for_test("customvoice")
    pack = tmp_path / "pack"
    (pack / "customvoice").mkdir(parents=True)
    (pack / "shared").mkdir()
    (pack / "customvoice" / "model.safetensors").write_bytes(WEIGHTS)
    (pack / "customvoice" / "config.json").write_bytes(b"cfg")
    (pack / "shared" / "vocab.json").write_bytes(SHARED_CONTENT)

    status = QwenModelManager(tmp_path / "root", "customvoice", profile).install_offline_pack(pack)

    assert status.state == "ready"
    assert (tmp_path / "root" / "shared" / "vocab.json").read_bytes() == SHARED_CONTENT

    (pack / "customvoice" / "config.json").write_bytes(b"wrong")
    corrupt = QwenModelManager(tmp_path / "root2", "customvoice", profile).install_offline_pack(
        pack
    )

    assert corrupt.state == "failed"
    assert "missing or corrupt" in corrupt.error

    (pack / "customvoice" / "config.json").write_bytes(b"cfg")
    (pack / "customvoice" / "extra.bin").write_bytes(b"x")
    extra = QwenModelManager(tmp_path / "root3", "customvoice", profile).install_offline_pack(pack)

    assert extra.state == "failed"
    assert "unexpected path" in extra.error
    assert not list((tmp_path / "root3").glob("*/install.json"))
