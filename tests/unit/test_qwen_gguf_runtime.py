"""Managed qwentts.cpp runtime packs (track qwen_gguf_engine_20260923).

The GGUF engine runs in an isolated native host whose libraries come from a
verified pack: a flat directory of shared objects, SONAME symlinks, license
notices and BUILD-INFO metadata, pinned per platform cell. These tests pin
the install contract — stage, verify every file and link against the locked
manifest, promote atomically — using tiny synthetic packs, never a real
native build.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from vienetts_app.core import qwen_gguf_runtime_manifest as manifest_module
from vienetts_app.core.qwen_gguf_runtime import QwenGgufRuntimeManager
from vienetts_app.core.qwen_gguf_runtime_manifest import (
    PackFile,
    PackLink,
    QwenGgufRuntimePack,
    host_cell_key,
    manifest_for_cell,
)

CELL = "linux-x64-cpu"

LIBQWEN = b"fake libqwen.so bytes"
GGML_BASE = b"fake libggml-base.so.0.23.0 bytes"
GGML_CPU = b"fake libggml-cpu-x64.so bytes"
LICENSE_TXT = b"MIT license text"


def _file(path: str, content: bytes) -> PackFile:
    return PackFile(path=path, size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())


def mini_pack(
    *,
    downloads: tuple[str, ...] = (),
    abi_version: int = 5,
) -> QwenGgufRuntimePack:
    return QwenGgufRuntimePack(
        cell=CELL,
        device="cpu",
        ggml_backend="CPU",
        abi_version=abi_version,
        library="libqwen.so",
        upstream_repo="ServeurpersoCom/qwentts.cpp",
        upstream_commit="0cbde9b5d21aa2142efa5d62e14b97f197ac6f6d",
        ggml_commit="0af0d7d5f66a6976b259b292cb4e7dc60457aa45",
        deployment_floor="glibc >= 2.35",
        backends=("CPU",),
        dependencies=("glibc >= 2.35", "libstdc++", "libm"),
        files=(
            _file("libqwen.so", LIBQWEN),
            _file("libggml-base.so.0.23.0", GGML_BASE),
            _file("libggml-cpu-x64.so", GGML_CPU),
            _file("licenses/qwentts.cpp-MIT.txt", LICENSE_TXT),
        ),
        links=(
            PackLink(path="libggml-base.so", target="libggml-base.so.0"),
            PackLink(path="libggml-base.so.0", target="libggml-base.so.0.23.0"),
        ),
        downloads=downloads,
    )


def pack_contents(pack: QwenGgufRuntimePack) -> dict[str, bytes]:
    return {
        "libqwen.so": LIBQWEN,
        "libggml-base.so.0.23.0": GGML_BASE,
        "libggml-cpu-x64.so": GGML_CPU,
        "licenses/qwentts.cpp-MIT.txt": LICENSE_TXT,
    }


def write_pack_dir(root: Path, pack: QwenGgufRuntimePack, contents: dict[str, bytes]) -> Path:
    """Materialize a staged pack directory (files + declared links)."""
    pack_dir = root / "pack"
    for record in pack.files:
        target = pack_dir / record.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[record.path])
    for link in pack.links:
        os.symlink(link.target, pack_dir / link.path)
    return pack_dir


def active_dir(manager: QwenGgufRuntimeManager) -> Path:
    return manager.root / manager.pack.cell / manager.pack.format_version


def staging_dir(manager: QwenGgufRuntimeManager) -> Path:
    return manager.root / ".staging" / manager.pack.cell / manager.pack.format_version


def file_downloader(payload: dict[str, bytes], calls: list[str] | None = None):
    """Serve pack files by their manifest path (the URL's trailing part)."""

    def download(url: str, target: Path) -> None:
        path = url.rsplit("/", 1)[-1]
        # Nested pack paths arrive percent-encoded by the caller.
        key = next((k for k in payload if k.rsplit("/", 1)[-1] == path), path)
        if calls is not None:
            calls.append(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload[key])

    return download


def test_install_from_offline_pack_promotes_a_verified_pack(tmp_path: Path) -> None:
    pack = mini_pack()
    pack_dir = write_pack_dir(tmp_path, pack, pack_contents(pack))
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)

    status = manager.install_from_offline_pack(pack_dir)

    assert status.ready
    assert status.runtime_identity == pack.identity
    assert status.library_path is not None
    assert status.library_path.is_relative_to(manager.root)
    assert status.library_path.name == "libqwen.so"
    active = active_dir(manager)
    assert (active / "libggml-base.so.0").is_symlink()
    assert os.readlink(active / "libggml-base.so.0") == "libggml-base.so.0.23.0"
    assert (active / "licenses" / "qwentts.cpp-MIT.txt").is_file()
    assert (active / "install.json").is_file()
    assert status.progress == 1.0


def test_status_reports_the_locked_identity_without_loading(tmp_path: Path) -> None:
    pack = mini_pack()
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    assert manager.status().state == "unavailable"

    write_pack_dir(tmp_path, pack, pack_contents(pack))
    manager.install_from_offline_pack(tmp_path / "pack")

    status = manager.status()
    assert status.ready
    assert status.runtime_identity == pack.identity
    assert status.location is not None
    assert status.location.abi_version == 5
    assert status.location.backends == ("CPU",)
    assert status.location.deployment_floor == "glibc >= 2.35"
    assert status.installed_bytes == pack.total_bytes


def test_online_install_downloads_verifies_and_promotes(tmp_path: Path) -> None:
    pack = mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",))
    calls: list[str] = []
    manager = QwenGgufRuntimeManager(
        tmp_path / "runtime",
        pack,
        downloader=file_downloader(pack_contents(pack), calls),
    )

    status = manager.install_online()

    assert status.ready
    assert status.runtime_identity == pack.identity
    assert sorted(calls) == sorted(pack_contents(pack))


def test_online_install_without_published_artifacts_is_an_honest_failure(
    tmp_path: Path,
) -> None:
    # Publication is a deferred release action; a cell without download URLs
    # reports that instead of pretending an install path exists.
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", mini_pack())
    status = manager.install_online()
    assert status.state == "failed"
    assert "no published runtime pack" in status.error
    assert manager.status().state == "unavailable"


def test_checksum_mismatch_never_promotes(tmp_path: Path) -> None:
    pack = mini_pack()
    payload = pack_contents(pack)
    payload["libqwen.so"] = b"not the locked bytes"
    manager = QwenGgufRuntimeManager(
        tmp_path / "runtime",
        mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",)),
        downloader=file_downloader(payload),
    )

    status = manager.install_online()

    assert status.state == "failed"
    assert "checksum mismatch" in status.error
    assert not active_dir(manager).exists()


def test_a_wrong_sized_file_never_promotes(tmp_path: Path) -> None:
    pack = mini_pack()
    pack_dir = write_pack_dir(tmp_path, pack, pack_contents(pack))
    (pack_dir / "libqwen.so").write_bytes(LIBQWEN + b"extra")
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)

    status = manager.install_from_offline_pack(pack_dir)

    assert status.state == "failed"
    assert "libqwen.so" in status.error


def test_an_offline_pack_with_a_symlinked_library_is_rejected(tmp_path: Path) -> None:
    pack = mini_pack()
    pack_dir = write_pack_dir(tmp_path, pack, pack_contents(pack))
    real = tmp_path / "elsewhere.so"
    real.write_bytes(LIBQWEN)
    (pack_dir / "libqwen.so").unlink()
    os.symlink(real, pack_dir / "libqwen.so")
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)

    status = manager.install_from_offline_pack(pack_dir)

    assert status.state == "failed"
    assert "libqwen.so" in status.error


def test_an_offline_pack_missing_a_file_is_rejected(tmp_path: Path) -> None:
    pack = mini_pack()
    pack_dir = write_pack_dir(tmp_path, pack, pack_contents(pack))
    (pack_dir / "libggml-cpu-x64.so").unlink()
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)

    status = manager.install_from_offline_pack(pack_dir)

    assert status.state == "failed"
    assert "libggml-cpu-x64.so" in status.error


def test_a_cancelled_install_keeps_partials_and_never_promotes(tmp_path: Path) -> None:
    from vienetts_app.core.managed_install import DownloadCancelled

    pack = mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",))
    seen: list[str] = []

    def cancelling_downloader(url: str, target: Path) -> None:
        path = url.rsplit("/", 1)[-1]
        seen.append(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if len(seen) >= 2:
            target.write_bytes(b"truncated-")
            raise DownloadCancelled
        target.write_bytes(pack_contents(pack)[path])

    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack, downloader=cancelling_downloader)
    status = manager.install_online()
    assert status.state == "unavailable"
    assert not (active_dir(manager) / "install.json").exists()
    # The completed file and the partial both survive for a later resume.
    staging = staging_dir(manager)
    assert (staging / "libqwen.so").read_bytes() == LIBQWEN
    assert (staging / "libggml-base.so.0.23.0").read_bytes() == b"truncated-"


def test_cancellation_mid_download_reports_unavailable(tmp_path: Path) -> None:
    pack = mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",))
    calls: list[str] = []

    contents = pack_contents(pack)

    def download(url: str, target: Path) -> None:
        path = url.rsplit("/", 1)[-1]
        key = next(k for k in contents if k.rsplit("/", 1)[-1] == path)
        calls.append(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[key])

    cancel_after = {"n": 2}
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack, downloader=download)
    status = manager.install_online(cancelled=lambda: len(calls) >= cancel_after["n"])
    assert status.state == "unavailable"
    # A retry resumes: staged verified files are not re-downloaded.
    calls.clear()
    status = manager.install_online()
    assert status.ready
    assert "libqwen.so" not in calls or len(calls) < len(pack.files)


def test_insufficient_disk_space_refuses_before_downloading(tmp_path: Path) -> None:
    pack = mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",))

    class Usage:
        free = 1

    manager = QwenGgufRuntimeManager(
        tmp_path / "runtime",
        pack,
        downloader=file_downloader(pack_contents(pack)),
        disk_usage=lambda _path: Usage(),
    )
    status = manager.install_online()
    assert status.state == "failed"
    assert "insufficient disk space" in status.error


def test_a_failed_promotion_keeps_the_previous_install(tmp_path: Path) -> None:
    pack = mini_pack()
    write_pack_dir(tmp_path, pack, pack_contents(pack))
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    assert manager.install_from_offline_pack(tmp_path / "pack").ready

    # A post-promotion check that always rejects forces a rollback mid-repair:
    # the previous install must come back byte-for-byte.
    def refusing_check(_root: Path) -> tuple[bool, str]:
        return False, "the host refused the pack"

    checked = QwenGgufRuntimeManager(tmp_path / "runtime", pack, verify_install=refusing_check)
    status = checked.repair()
    assert status.state == "failed"
    assert "the host refused the pack" in status.error

    current = checked.status()
    assert current.ready  # the good install survived untouched
    assert current.runtime_identity == pack.identity


def test_a_promoted_pack_that_fails_its_check_is_not_kept(tmp_path: Path) -> None:
    pack = mini_pack()
    write_pack_dir(tmp_path, pack, pack_contents(pack))
    manager = QwenGgufRuntimeManager(
        tmp_path / "runtime",
        pack,
        verify_install=lambda _root: (False, "qt_version failed"),
    )
    status = manager.install_from_offline_pack(tmp_path / "pack")
    assert status.state == "failed"
    assert "qt_version failed" in status.error
    assert not active_dir(manager).exists()  # nothing half-installed is exposed


def test_remove_refuses_while_in_use(tmp_path: Path) -> None:
    pack = mini_pack()
    write_pack_dir(tmp_path, pack, pack_contents(pack))
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    manager.install_from_offline_pack(tmp_path / "pack")

    status = manager.remove(in_use=True)
    assert status.state == "failed"
    assert "in use" in status.error
    assert manager.status().ready

    assert manager.remove().state == "unavailable"
    assert not active_dir(manager).exists()


def test_repair_replaces_only_corrupt_staged_files(tmp_path: Path) -> None:
    pack = mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",))
    manager = QwenGgufRuntimeManager(
        tmp_path / "runtime",
        pack,
        downloader=file_downloader(pack_contents(pack)),
    )
    assert manager.install_online().ready

    # Damage the installed pack — a deleted file and a corrupt one.
    active = active_dir(manager)
    (active / "libggml-cpu-x64.so").unlink()
    (active / "libqwen.so").write_bytes(b"tampered")

    status = manager.status()
    assert status.state == "failed"

    status = manager.repair()
    assert status.ready
    assert (active / "libqwen.so").read_bytes() == LIBQWEN


def test_cancel_staging_removes_only_the_staging_tree(tmp_path: Path) -> None:
    pack = mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",))
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    staging = staging_dir(manager)
    (staging / "libqwen.so").parent.mkdir(parents=True, exist_ok=True)
    (staging / "libqwen.so").write_bytes(b"partial")
    manager.cancel_staging()
    assert not (manager.root / ".staging").exists() or not list(
        (manager.root / ".staging").rglob("*")
    )


class TestManifestData:
    """The shipped manifest is the install contract for every locked cell."""

    def test_the_shipped_manifest_matches_the_locked_pack(self) -> None:
        pack = manifest_for_cell(CELL)
        assert pack is not None
        locked = json.loads(
            Path("packaging/qwen-gguf-pack-manifests.json").read_text(encoding="utf-8")
        )
        locked_pack = locked["packs"][CELL]
        assert pack.upstream_commit == locked_pack["upstream"]["commit"]
        assert pack.ggml_commit == locked_pack["upstream"]["ggmlSubmoduleCommit"]
        assert pack.abi_version == locked_pack["abiVersion"]
        assert {f.path for f in pack.files} == {f["path"] for f in locked_pack["files"]}
        for record in pack.files:
            locked_file = next(f for f in locked_pack["files"] if f["path"] == record.path)
            assert record.sha256 == locked_file["sha256"]
            assert record.size_bytes == locked_file["size"]
        assert {(lnk.path, lnk.target) for lnk in pack.links} == {
            (lnk["path"], lnk["target"]) for lnk in locked_pack["links"]
        }

    def test_only_verified_cells_ship(self) -> None:
        # linux-x64-cpu is the only probed+locked cell; the others stay absent
        # rather than shipping unverified install recipes.
        assert manifest_for_cell(CELL) is not None
        for cell in (
            "windows-x64-cpu",
            "windows-x64-cuda",
            "linux-x64-cuda",
            "macos-arm64-cpu",
            "macos-arm64-metal",
        ):
            assert manifest_for_cell(cell) is None

    def test_host_cell_key_maps_devices_to_cells(self, monkeypatch) -> None:
        monkeypatch.setattr(manifest_module.sys, "platform", "linux")
        monkeypatch.setattr(manifest_module.platform, "machine", lambda: "x86_64")
        assert host_cell_key("cpu") == "linux-x64-cpu"
        assert host_cell_key("cuda") == "linux-x64-cuda"
        assert host_cell_key("metal") is None  # no Metal on Linux

        monkeypatch.setattr(manifest_module.sys, "platform", "darwin")
        monkeypatch.setattr(manifest_module.platform, "machine", lambda: "arm64")
        assert host_cell_key("metal") == "macos-arm64-metal"
        assert host_cell_key("cpu") == "macos-arm64-cpu"
        assert host_cell_key("cuda") is None  # no CUDA on macOS

    def test_pack_identity_is_deterministic_and_cell_scoped(self) -> None:
        pack = mini_pack()
        again = mini_pack()
        assert pack.identity == again.identity
        other = dataclasses.replace(pack, cell="windows-x64-cpu")
        assert other.identity != pack.identity
        assert "linux-x64-cpu" in pack.identity


class TestManifestValidation:
    """Loader strictness: a malformed manifest can never produce a pack."""

    def _data(self, pack: QwenGgufRuntimePack, **overrides: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "cell": pack.cell,
            "device": pack.device,
            "ggmlBackend": pack.ggml_backend,
            "abiVersion": pack.abi_version,
            "library": pack.library,
            "upstream": {
                "repo": pack.upstream_repo,
                "commit": pack.upstream_commit,
                "ggmlSubmoduleCommit": pack.ggml_commit,
            },
            "deploymentFloor": pack.deployment_floor,
            "backends": list(pack.backends),
            "dependencies": list(pack.dependencies),
            "files": [
                {"path": f.path, "size": f.size_bytes, "sha256": f.sha256} for f in pack.files
            ],
            "links": [{"path": lnk.path, "target": lnk.target} for lnk in pack.links],
            "downloads": list(pack.downloads),
        }
        entry.update(overrides)
        return {"formatVersion": "qwen-gguf-runtime-v1", "cells": {pack.cell: entry}}

    def test_a_valid_manifest_loads(self) -> None:
        loaded = manifest_module.load_manifests_from_data(self._data(mini_pack()))
        assert loaded[CELL].library == "libqwen.so"

    def test_wrong_format_version_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="format"):
            manifest_module.load_manifests_from_data(
                {**self._data(mini_pack()), "formatVersion": "v0"}
            )

    def test_an_unknown_cell_is_rejected(self) -> None:
        data = self._data(mini_pack())
        data["cells"]["plan9-mips-cpu"] = data["cells"].pop(CELL)
        with pytest.raises(ValueError, match="cell"):
            manifest_module.load_manifests_from_data(data)

    def test_an_abi_below_the_floor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="abi"):
            manifest_module.load_manifests_from_data(self._data(mini_pack(), abiVersion=4))

    def test_a_missing_library_file_is_rejected(self) -> None:
        pack = mini_pack()
        data = self._data(pack)
        data["cells"][CELL]["files"] = [
            f for f in data["cells"][CELL]["files"] if f["path"] != "libqwen.so"
        ]
        with pytest.raises(ValueError, match="libqwen.so"):
            manifest_module.load_manifests_from_data(data)

    def test_a_traversing_file_path_is_rejected(self) -> None:
        data = self._data(mini_pack())
        data["cells"][CELL]["files"].append({"path": "../escape.so", "size": 1, "sha256": "0" * 64})
        with pytest.raises(ValueError, match="path"):
            manifest_module.load_manifests_from_data(data)

    def test_an_absolute_file_path_is_rejected(self) -> None:
        data = self._data(mini_pack())
        data["cells"][CELL]["files"].append({"path": "/etc/passwd", "size": 1, "sha256": "0" * 64})
        with pytest.raises(ValueError, match="path"):
            manifest_module.load_manifests_from_data(data)

    def test_a_link_to_an_undeclared_file_is_rejected(self) -> None:
        data = self._data(mini_pack())
        data["cells"][CELL]["links"].append(
            {"path": "libevil.so", "target": "/lib/x86_64-linux-gnu/libc.so.6"}
        )
        with pytest.raises(ValueError, match="link"):
            manifest_module.load_manifests_from_data(data)

    def test_a_link_pointing_outside_the_pack_is_rejected(self) -> None:
        data = self._data(mini_pack())
        data["cells"][CELL]["links"] = [{"path": "libescape.so", "target": "../outside.so"}]
        with pytest.raises(ValueError, match="link"):
            manifest_module.load_manifests_from_data(data)

    def test_a_bad_sha256_is_rejected(self) -> None:
        data = self._data(mini_pack())
        data["cells"][CELL]["files"][0]["sha256"] = "not-hex"
        with pytest.raises(ValueError, match="sha"):
            manifest_module.load_manifests_from_data(data)

    def test_a_download_url_must_be_https_from_an_allowed_host(self) -> None:
        pack = mini_pack(downloads=("http://github.com/acme/packs/x",))
        with pytest.raises(ValueError, match="HTTPS|https"):
            manifest_module.load_manifests_from_data(self._data(pack))
        pack = mini_pack(downloads=("https://evil.example.com/packs/x",))
        with pytest.raises(ValueError, match="host"):
            manifest_module.load_manifests_from_data(self._data(pack))

    def test_duplicate_file_paths_are_rejected(self) -> None:
        data = self._data(mini_pack())
        data["cells"][CELL]["files"].append(data["cells"][CELL]["files"][0])
        with pytest.raises(ValueError, match="duplicate"):
            manifest_module.load_manifests_from_data(data)


class FakeResponse:
    """Minimal streaming response for the HTTP path (mirrors the wheel tests)."""

    def __init__(self, content: bytes, *, status: int = 200, url: str | None = None) -> None:
        self._buffer = io.BytesIO(content)
        self.status = status
        self.headers: dict[str, str] = {}
        self.url = url

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str | None:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_http_download_resumes_a_partial_file(tmp_path: Path) -> None:
    pack = mini_pack(downloads=("https://github.com/acme/packs/linux-x64-cpu",))
    contents = pack_contents(pack)

    def opener(request: Any, timeout: float = 0) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        path = url.rsplit("/", 1)[-1]
        key = next(k for k in contents if k.rsplit("/", 1)[-1] == path)
        body = contents[key]
        range_header = getattr(request, "headers", {}).get("Range")
        if range_header:
            offset = int(range_header.removeprefix("bytes=").split("-")[0])
            response = FakeResponse(body[offset:], status=206, url=url)
            response.headers["Content-Range"] = f"bytes {offset}-{len(body) - 1}/{len(body)}"
            return response
        return FakeResponse(body, url=url)

    # Seed a truncated libqwen download, then let the install resume it.
    staging_file = tmp_path / "runtime" / ".staging" / CELL / pack.format_version / "libqwen.so"
    staging_file.parent.mkdir(parents=True, exist_ok=True)
    staging_file.write_bytes(LIBQWEN[:4])

    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack, opener=opener)
    status = manager.install_online()
    assert status.ready
    assert (active_dir(manager) / "libqwen.so").read_bytes() == LIBQWEN


def test_a_pack_for_another_platform_is_never_installed(tmp_path: Path) -> None:
    # A Windows pack would verify byte-for-byte on Linux and then fail every
    # load — the platform guard refuses it before any disk work.
    windows_pack = dataclasses.replace(
        mini_pack(),
        cell="windows-x64-cpu",
        library="qwen.dll",
        files=(
            _file("qwen.dll", LIBQWEN),
            *tuple(f for f in mini_pack().files if f.path != "libqwen.so"),
        ),
        links=(),
    )
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", windows_pack)
    status = manager.install_from_offline_pack(tmp_path / "nowhere")
    assert status.state == "failed"
    assert "windows-x64-cpu" in status.error


def test_install_from_a_zip_pack_archive(tmp_path: Path) -> None:
    import zipfile

    pack = mini_pack()
    archive = tmp_path / "qwen-gguf-linux-x64-cpu.zip"
    contents = pack_contents(pack)
    with zipfile.ZipFile(archive, "w") as zf:
        # A zip of the pack dir carries members under a shared prefix; links
        # arrive as plain copies and are recreated properly at install time.
        for path, data in contents.items():
            zf.writestr(f"pack/{path}", data)
        for link in pack.links:
            target = link.target
            # Resolve through the link chain to the terminal file's bytes.
            by_path = {lnk.path: lnk.target for lnk in pack.links}
            while target in by_path:
                target = by_path[target]
            zf.writestr(f"pack/{link.path}", contents[target])

    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    status = manager.install_from_offline_pack(archive)
    assert status.ready
    assert (active_dir(manager) / "libggml-base.so.0").is_symlink()


def test_a_tar_gz_pack_archive_installs(tmp_path: Path) -> None:
    import tarfile

    pack = mini_pack()
    archive = tmp_path / "pack.tar.gz"
    contents = pack_contents(pack)
    with tarfile.open(archive, "w:gz") as tf:
        for path, data in contents.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    status = manager.install_from_offline_pack(archive)
    assert status.ready


def test_an_archive_missing_a_member_is_rejected(tmp_path: Path) -> None:
    import zipfile

    pack = mini_pack()
    archive = tmp_path / "pack.zip"
    contents = pack_contents(pack)
    del contents["libqwen.so"]
    with zipfile.ZipFile(archive, "w") as zf:
        for path, data in contents.items():
            zf.writestr(path, data)
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    status = manager.install_from_offline_pack(archive)
    assert status.state == "failed"
    assert "libqwen.so" in status.error


def test_a_corrupt_archive_is_rejected(tmp_path: Path) -> None:
    pack = mini_pack()
    archive = tmp_path / "pack.zip"
    archive.write_bytes(b"this is not a zip")
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    status = manager.install_from_offline_pack(archive)
    assert status.state == "failed"
    assert "unreadable" in status.error


def test_remove_cleans_everything(tmp_path: Path) -> None:
    pack = mini_pack()
    write_pack_dir(tmp_path, pack, pack_contents(pack))
    manager = QwenGgufRuntimeManager(tmp_path / "runtime", pack)
    manager.install_from_offline_pack(tmp_path / "pack")
    assert manager.remove().state == "unavailable"
    assert not active_dir(manager).exists()
