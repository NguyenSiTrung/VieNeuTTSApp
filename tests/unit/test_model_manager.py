"""ModelManager staging tests (Phase 1 Task 2, TDD RED)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from vienetts_app.core.official_model_manifest import (
    ModelFile,
    OfficialModelManifest,
)

CONTENT = {
    "a.txt": b"hello",
    "b.bin": b"world!",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mini_manifest() -> OfficialModelManifest:
    return OfficialModelManifest(
        format_version="official-v1",
        backbone_repo="test-backbone",
        backbone_revision="rev-backbone",
        codec_repo="test-codec",
        codec_revision="rev-codec",
        files=(
            ModelFile("backbone", "a.txt", len(CONTENT["a.txt"]), _sha(CONTENT["a.txt"])),
            ModelFile("codec", "b.bin", len(CONTENT["b.bin"]), _sha(CONTENT["b.bin"])),
        ),
    )


def materialize_requested_file(
    *,
    repo_id: str,
    filename: str,
    revision: str,
    local_dir: str,
    local_dir_use_symlinks: bool = False,
) -> Path:
    assert local_dir_use_symlinks is False
    path = Path(local_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CONTENT[filename])
    return path


def _staging_file(manager, repo_dir: str, name: str) -> Path:
    return manager.root / ".staging" / "official-v1" / repo_dir / name


def test_incomplete_staging_is_not_reported_as_ready(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    def exploding_downloader(**_kwargs) -> Path:
        raise AssertionError("download invoked")

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=exploding_downloader,
    )
    staging = manager.root / ".staging" / "official-v1"
    (staging / "backbone").mkdir(parents=True)
    (staging / "backbone" / "a.txt").write_bytes(b"wrong")

    status = manager.inspect()

    assert status.state == "unavailable"
    assert status.location is None


def test_validated_staging_is_promoted_atomically(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=materialize_requested_file,
    )

    status = manager.install()

    assert status.state == "ready"
    assert status.location is not None
    assert (status.location.root / "install.json").is_file()
    assert not (manager.root / ".staging" / "official-v1").exists()
    payload = json.loads((status.location.root / "install.json").read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    assert str(tmp_path) not in blob
    assert "token" not in blob.lower()
    assert payload["format"] == "official-v1"


def test_checksum_failure_never_creates_an_active_install(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    def corrupt_downloader(**kwargs) -> Path:
        target = Path(kwargs["local_dir"]) / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"corrupt")
        return target

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=corrupt_downloader,
    )

    status = manager.install()

    assert status.state == "failed"
    assert status.location is None
    assert not (manager.root / "official-v1" / "install.json").exists()


def test_low_disk_space_fails_without_downloading(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    manifest = mini_manifest()
    calls: list = []

    def counting_downloader(**kwargs) -> Path:
        calls.append(kwargs)
        return materialize_requested_file(**kwargs)

    def tiny_disk(_path: Path):
        return SimpleNamespace(total=10, used=9, free=1)

    manager = ModelManager(
        tmp_path / "models",
        manifest=manifest,
        downloader=counting_downloader,
        disk_usage=tiny_disk,
    )
    assert manifest.required_free_bytes > 1

    status = manager.install()

    assert status.state == "failed"
    assert calls == []
    assert not (manager.root / "official-v1" / "install.json").exists()


def test_cancellation_after_first_file_preserves_verified_staging(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    manifest = mini_manifest()
    calls: list = []

    def recording_downloader(**kwargs) -> Path:
        calls.append(kwargs["filename"])
        return materialize_requested_file(**kwargs)

    manager = ModelManager(
        tmp_path / "models",
        manifest=manifest,
        downloader=recording_downloader,
    )
    # Pre-seed the first file as already verified in staging.
    first = _staging_file(manager, "backbone", "a.txt")
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(CONTENT["a.txt"])

    seen = {"n": 0}

    def cancelled() -> bool:
        seen["n"] += 1
        return seen["n"] > 1

    status = manager.install(cancelled=cancelled)

    assert status.state == "unavailable"
    assert "b.bin" not in calls
    assert _staging_file(manager, "backbone", "a.txt").is_file()
    assert not (manager.root / "official-v1" / "install.json").exists()


def test_valid_staging_file_skips_redownload(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    downloaded: list = []

    def recording_downloader(**kwargs) -> Path:
        downloaded.append(kwargs["filename"])
        return materialize_requested_file(**kwargs)

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=recording_downloader,
    )
    first = _staging_file(manager, "backbone", "a.txt")
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(CONTENT["a.txt"])

    status = manager.install()

    assert status.state == "ready"
    assert downloaded == ["b.bin"]


def test_offline_pack_rejects_extra_file_and_promotes_valid_pack(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    def exploding_downloader(**_kwargs) -> Path:
        raise AssertionError("download invoked")

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=exploding_downloader,
    )
    source = tmp_path / "pack"
    (source / "backbone").mkdir(parents=True)
    (source / "codec").mkdir(parents=True)
    (source / "backbone" / "a.txt").write_bytes(CONTENT["a.txt"])
    (source / "codec" / "b.bin").write_bytes(CONTENT["b.bin"])
    (source / "backbone" / "evil.txt").write_bytes(b"x")

    status = manager.install_offline_pack(source)

    assert status.state == "failed"
    assert not (manager.root / "official-v1" / "install.json").exists()

    (source / "backbone" / "evil.txt").unlink()
    status = manager.install_offline_pack(source)

    assert status.state == "ready"
    assert status.location is not None
    assert (status.location.root / "install.json").is_file()


def test_progress_callback_reports_file_fraction(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    seen: list = []
    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=materialize_requested_file,
    )

    status = manager.install(on_progress=seen.append)

    assert status.state == "ready"
    assert status.progress == 1.0
    assert seen, "expected progress callbacks"
    assert seen[-1].progress == 1.0


def test_subfolder_manifest_files_download_with_repo_root_local_dir(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    nested_content = b'{"model": "onnx"}'
    manifest = OfficialModelManifest(
        format_version="official-v1",
        backbone_repo="test-backbone",
        backbone_revision="rev-backbone",
        codec_repo="test-codec",
        codec_revision="rev-codec",
        files=(
            ModelFile(
                "backbone",
                "onnx_int8/config.json",
                len(nested_content),
                _sha(nested_content),
            ),
        ),
    )
    recorded_calls: list[dict] = []

    def fake_downloader(**kwargs) -> Path:
        recorded_calls.append(kwargs)
        # Simulate huggingface_hub creating .cache inside local_dir
        cache_file = (
            Path(kwargs["local_dir"]) / ".cache" / "huggingface" / "download" / "dummy.incomplete"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"temp")

        target = Path(kwargs["local_dir"]) / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(nested_content)
        return target

    manager = ModelManager(
        tmp_path / "models",
        manifest=manifest,
        downloader=fake_downloader,
    )
    status = manager.install()

    assert status.state == "ready"
    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    expected_local_dir = str(manager.root / ".staging" / "official-v1" / "backbone")
    assert call["local_dir"] == expected_local_dir
    assert call["filename"] == "onnx_int8/config.json"

    # Promoted install should have the file, but NO .cache metadata directory
    active_backbone = status.location.root / "backbone"
    assert (active_backbone / "onnx_int8" / "config.json").is_file()
    assert not (active_backbone / ".cache").exists()


def test_windows_extended_path_used_when_staging_path_is_long(tmp_path: Path, monkeypatch) -> None:
    import os

    from vienetts_app.core.model_manager import ModelManager

    recorded_local_dirs: list[str] = []

    def fake_downloader(**kwargs) -> Path:
        recorded_local_dirs.append(kwargs["local_dir"])
        repo_dir = "backbone" if kwargs["filename"] == "a.txt" else "codec"
        target = tmp_path / "models" / ".staging" / "official-v1" / repo_dir / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENT[kwargs["filename"]])
        return target

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=fake_downloader,
    )

    monkeypatch.setattr(os, "name", "nt")
    fake_long_win_path = "C:\\" + ("sub\\" * 60)
    original_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if ".staging" in str(self):
            # Return an object whose string conversion is the long Windows path
            class LongPath:
                def __str__(self):
                    return fake_long_win_path

            return LongPath()
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    status = manager.install()
    assert status.state == "ready", status.error
    assert len(recorded_local_dirs) == 2
    assert recorded_local_dirs[0].startswith("\\\\?\\C:\\")


def test_is_same_file_handles_windows_extended_prefix() -> None:
    # Moved to core.managed_install (Phase 2 Task 1); the manager still relies
    # on exactly this Windows path handling for its downloader co-location.
    from vienetts_app.core.managed_install import is_same_file, normalize_windows_path

    assert normalize_windows_path(r"\\?\C:/Users/test/a.txt") == r"C:\Users\test\a.txt"
    assert normalize_windows_path(r"\\?\UNC\server/share/test.txt") == r"\\server\share\test.txt"

    p1 = Path(r"\\?\C:\Users\test\backbone\a.txt")
    p2 = Path(r"C:\Users\test\backbone\a.txt")
    assert is_same_file(p1, p2)

    p3 = Path(r"\\?\C:/Users/test/backbone/a.txt")
    assert is_same_file(p3, p2)

    p4 = Path(r"C:\Users\test\backbone\other.txt")
    assert not is_same_file(p1, p4)


def test_windows_extended_path_downloader_return_promotes_ready(tmp_path: Path) -> None:
    from vienetts_app.core.model_manager import ModelManager

    def extended_path_downloader(**kwargs) -> Path:
        repo_dir = "backbone" if kwargs["filename"] == "a.txt" else "codec"
        target = tmp_path / "models" / ".staging" / "official-v1" / repo_dir / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENT[kwargs["filename"]])
        return Path(f"\\\\?\\{target.resolve()}")

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=extended_path_downloader,
    )

    status = manager.install()
    assert status.state == "ready"
    assert status.location is not None
    assert (status.location.root / "backbone" / "a.txt").is_file()


def test_windows_extended_path_used_unconditionally_even_for_short_staging_path(
    tmp_path: Path, monkeypatch
) -> None:
    import os

    from vienetts_app.core.model_manager import ModelManager

    recorded_local_dirs: list[str] = []

    def fake_downloader(**kwargs) -> Path:
        recorded_local_dirs.append(kwargs["local_dir"])
        repo_dir = "backbone" if kwargs["filename"] == "a.txt" else "codec"
        target = tmp_path / "models" / ".staging" / "official-v1" / repo_dir / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENT[kwargs["filename"]])
        return target

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=fake_downloader,
    )

    monkeypatch.setattr(os, "name", "nt")
    original_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if ".staging" in str(self):

            class ShortWinPath:
                def __str__(self):
                    return r"C:\m\.staging\official-v1\backbone"

            return ShortWinPath()
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    status = manager.install()
    assert status.state == "ready", status.error
    assert len(recorded_local_dirs) == 2
    assert all(d.startswith("\\\\?\\C:\\") for d in recorded_local_dirs)


def test_model_manager_stamp_ledger_skips_warm_and_post_promotion_hashing(
    tmp_path: Path, monkeypatch
) -> None:
    import os

    import pytest

    from vienetts_app.core import managed_install as mi
    from vienetts_app.core.model_manager import ModelManager

    hash_calls: list[Path] = []
    real_hash = mi.sha256_of
    monkeypatch.setattr(mi, "sha256_of", lambda p: hash_calls.append(p) or real_hash(p))

    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=materialize_requested_file,
    )
    status = manager.install()
    assert status.state == "ready"
    # Fresh stamps are written before promotion so post-promotion inspect does not re-hash.
    assert len(hash_calls) == len(mini_manifest().files)
    install_path = status.location.root / "install.json"
    meta = json.loads(install_path.read_text(encoding="utf-8"))
    assert set(meta["stamps"]) == {"backbone/a.txt", "codec/b.bin"}

    # Warm stamped startup performs 0 full SHA-256 reads.
    hash_calls.clear()
    assert manager.inspect().state == "ready"
    assert manager.inspect(mode="stamp").state == "ready"
    assert hash_calls == []

    # Force full verification hashes every file regardless of stamps.
    assert manager.inspect(mode="full").state == "ready"
    assert len(hash_calls) == len(mini_manifest().files)

    # Touching a file rehashes only that file once and persists its refreshed stamp.
    hash_calls.clear()
    a_file = status.location.root / "backbone" / "a.txt"
    st = a_file.stat()
    os.utime(a_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert manager.inspect().state == "ready"
    assert manager.inspect().state == "ready"
    assert hash_calls == [a_file]

    # Legacy install.json without stamps hashes once, then persists stamps.
    hash_calls.clear()
    del meta["stamps"]
    install_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    assert manager.inspect().state == "ready"
    assert manager.inspect().state == "ready"
    assert len(hash_calls) == len(mini_manifest().files)
    assert set(json.loads(install_path.read_text(encoding="utf-8"))["stamps"]) == {
        "backbone/a.txt",
        "codec/b.bin",
    }

    # Corrupted file fails inspect and strips its stamp from install.json.
    hash_calls.clear()
    a_file.write_bytes(b"XXXXX")
    st2 = a_file.stat()
    os.utime(a_file, ns=(st2.st_atime_ns, st2.st_mtime_ns + 2_000_000))
    failed = manager.inspect()
    assert failed.state == "failed"
    assert "backbone/a.txt" not in json.loads(install_path.read_text(encoding="utf-8"))["stamps"]

    with pytest.raises(ValueError, match="verification mode"):
        manager.inspect(mode="bogus")


def test_offline_pack_persists_stamps_for_zero_hash_warm_inspect(
    tmp_path: Path, monkeypatch
) -> None:
    from vienetts_app.core import managed_install as mi
    from vienetts_app.core.model_manager import ModelManager

    source = tmp_path / "pack"
    (source / "backbone").mkdir(parents=True)
    (source / "codec").mkdir(parents=True)
    (source / "backbone" / "a.txt").write_bytes(CONTENT["a.txt"])
    (source / "codec" / "b.bin").write_bytes(CONTENT["b.bin"])

    hash_calls: list[Path] = []
    real_hash = mi.sha256_of
    monkeypatch.setattr(mi, "sha256_of", lambda p: hash_calls.append(p) or real_hash(p))

    manager = ModelManager(tmp_path / "models", manifest=mini_manifest())
    status = manager.install_offline_pack(source)
    assert status.state == "ready"
    assert len(hash_calls) == len(mini_manifest().files)

    hash_calls.clear()
    assert manager.inspect().state == "ready"
    assert hash_calls == []
