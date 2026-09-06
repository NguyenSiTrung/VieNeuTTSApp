"""CUDA runtime installer lifecycle and security tests."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from vienetts_app.core.cuda_runtime import CudaRuntimeManager
from vienetts_app.core.cuda_runtime_manifest import CudaRuntimeManifest, RuntimeWheel


def wheel_bytes(*members: tuple[str, bytes] | zipfile.ZipInfo) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member in members:
            if isinstance(member, zipfile.ZipInfo):
                archive.writestr(member, b"target")
            else:
                archive.writestr(*member)
    return buffer.getvalue()


CONTENT = wheel_bytes(("demo/__init__.py", b""))


def mini_manifest(content: bytes = CONTENT) -> CudaRuntimeManifest:
    wheel = RuntimeWheel(
        "demo-1.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/a/demo-1.0-py3-none-any.whl",
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    return CudaRuntimeManifest("test-v1", "linux-x64", "cp313", (wheel,))


def write_wheel(content: bytes):
    def download(_item: RuntimeWheel, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    return download


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        url: str | None = None,
        read_size: int = 1024,
    ) -> None:
        self._buffer = io.BytesIO(content)
        self.status = status
        self.headers = headers or {}
        self.url = url
        self.read_size = read_size

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(min(size, self.read_size) if size >= 0 else self.read_size)

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str | None:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_validated_staging_promotes_only_complete_runtime(tmp_path: Path) -> None:
    manager = CudaRuntimeManager(
        tmp_path, manifest=mini_manifest(), downloader=write_wheel(CONTENT)
    )

    status = manager.install()

    assert status.state == "ready"
    assert status.location is not None
    assert (status.location.root / "install.json").is_file()
    assert (status.location.site_packages / "demo" / "__init__.py").is_file()
    assert not (manager.root / ".staging" / status.location.format_version).exists()


def test_checksum_failure_never_creates_active_runtime(tmp_path: Path) -> None:
    manager = CudaRuntimeManager(tmp_path, manifest=mini_manifest(), downloader=write_wheel(b"bad"))

    status = manager.install()

    assert status.state == "failed"
    assert status.location is None
    assert not list(tmp_path.glob("*/install.json"))
    assert not (
        tmp_path / ".staging" / "test-v1" / "wheels" / "demo-1.0-py3-none-any.whl.part"
    ).exists()


def test_low_disk_space_fails_before_downloading(tmp_path: Path) -> None:
    calls: list[RuntimeWheel] = []

    def downloader(item: RuntimeWheel, target: Path) -> None:
        calls.append(item)
        target.write_bytes(CONTENT)

    manager = CudaRuntimeManager(
        tmp_path,
        manifest=mini_manifest(),
        downloader=downloader,
        disk_usage=lambda _path: SimpleNamespace(free=1),
    )

    status = manager.install()

    assert status.state == "failed"
    assert status.required_bytes > 1
    assert calls == []
    assert not (tmp_path / "test-v1").exists()


def test_cancellation_retains_verified_archive_without_active_runtime(tmp_path: Path) -> None:
    downloaded = False

    def downloader(_item: RuntimeWheel, target: Path) -> None:
        nonlocal downloaded
        target.write_bytes(CONTENT)
        downloaded = True

    manager = CudaRuntimeManager(tmp_path, manifest=mini_manifest(), downloader=downloader)

    status = manager.install(cancelled=lambda: downloaded)

    archive = tmp_path / ".staging" / "test-v1" / "wheels" / "demo-1.0-py3-none-any.whl.part"
    assert status.state == "unavailable"
    assert archive.read_bytes() == CONTENT
    assert not (tmp_path / "test-v1").exists()


def test_streaming_cancellation_stops_between_chunks_without_active_runtime(tmp_path: Path) -> None:
    manifest = mini_manifest()
    reads = 0

    class CancellingResponse(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            nonlocal reads
            reads += 1
            return super().read(size)

    def opener(_request, timeout: float):
        assert timeout > 0
        return CancellingResponse(
            CONTENT,
            url=manifest.wheels[0].url,
            read_size=1,
        )

    status = CudaRuntimeManager(tmp_path, manifest=manifest, opener=opener).install(
        cancelled=lambda: reads >= 1
    )

    archive = tmp_path / ".staging" / "test-v1" / "wheels" / f"{manifest.wheels[0].filename}.part"
    assert status.state == "unavailable"
    assert reads == 1
    assert not archive.exists()
    assert not (tmp_path / "test-v1").exists()


def test_built_in_downloader_resumes_only_after_honored_range(tmp_path: Path) -> None:
    manifest = mini_manifest()
    archive = tmp_path / ".staging" / "test-v1" / "wheels" / manifest.wheels[0].filename
    archive = archive.with_suffix(archive.suffix + ".part")
    archive.parent.mkdir(parents=True)
    split = len(CONTENT) // 2
    archive.write_bytes(CONTENT[:split])
    requests = []

    def opener(request, timeout: float):
        requests.append((request, timeout))
        return FakeResponse(
            CONTENT[split:],
            status=206,
            headers={"Content-Range": f"bytes {split}-{len(CONTENT) - 1}/{len(CONTENT)}"},
            url=manifest.wheels[0].url,
        )

    status = CudaRuntimeManager(tmp_path, manifest=manifest, opener=opener).install()

    assert status.state == "ready"
    assert requests[0][0].get_header("Range") == f"bytes={split}-"
    assert status.location is not None
    assert (status.location.site_packages / "demo" / "__init__.py").is_file()


def test_built_in_downloader_restarts_when_server_ignores_range(tmp_path: Path) -> None:
    manifest = mini_manifest()
    archive = tmp_path / ".staging" / "test-v1" / "wheels" / manifest.wheels[0].filename
    archive = archive.with_suffix(archive.suffix + ".part")
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not the archive")
    requests = []

    def opener(request, timeout: float):
        assert timeout > 0
        requests.append(request)
        return FakeResponse(CONTENT, url=manifest.wheels[0].url)

    status = CudaRuntimeManager(tmp_path, manifest=manifest, opener=opener).install()

    assert status.state == "ready"
    assert requests[0].get_header("Range") == f"bytes={len(b'not the archive')}-"


def test_invalid_full_length_partial_restarts_before_requesting_range(tmp_path: Path) -> None:
    manifest = mini_manifest()
    archive = tmp_path / ".staging" / "test-v1" / "wheels" / f"{manifest.wheels[0].filename}.part"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"x" * manifest.wheels[0].size_bytes)
    requests = []

    def opener(request, timeout: float):
        assert timeout > 0
        requests.append(request)
        return FakeResponse(CONTENT, url=manifest.wheels[0].url)

    status = CudaRuntimeManager(tmp_path, manifest=manifest, opener=opener).install()

    assert status.state == "ready"
    assert requests[0].get_header("Range") is None


@pytest.mark.parametrize("resume", [False, True])
def test_oversized_http_chunk_is_rejected_before_archive_write(
    tmp_path: Path,
    monkeypatch,
    resume: bool,
) -> None:
    manifest = mini_manifest()
    archive = tmp_path / ".staging" / "test-v1" / "wheels" / f"{manifest.wheels[0].filename}.part"
    split = len(CONTENT) // 2
    if resume:
        archive.parent.mkdir(parents=True)
        archive.write_bytes(CONTENT[:split])
    writes: list[int] = []
    original_open = Path.open

    class RecordingWriter:
        def __init__(self, file) -> None:
            self._file = file

        def __enter__(self):
            self._file.__enter__()
            return self

        def __exit__(self, *args) -> None:
            self._file.__exit__(*args)

        def write(self, data: bytes) -> int:
            writes.append(len(data))
            return self._file.write(data)

    def recording_open(path: Path, mode: str = "r", *args, **kwargs):
        file = original_open(path, mode, *args, **kwargs)
        if path == archive and mode in {"wb", "ab"}:
            return RecordingWriter(file)
        return file

    monkeypatch.setattr(Path, "open", recording_open)

    def opener(_request, timeout: float):
        assert timeout > 0
        headers = (
            {"Content-Range": f"bytes {split}-{len(CONTENT) - 1}/{len(CONTENT)}"} if resume else {}
        )
        return FakeResponse(
            CONTENT[split:] + b"excess" if resume else CONTENT + b"excess",
            status=206 if resume else 200,
            headers=headers,
            url=manifest.wheels[0].url,
        )

    status = CudaRuntimeManager(tmp_path, manifest=manifest, opener=opener).install()

    assert status.state == "failed"
    assert writes == []
    assert not archive.exists()


def test_built_in_downloader_rejects_redirected_response(tmp_path: Path) -> None:
    manifest = mini_manifest()

    def opener(_request, timeout: float):
        assert timeout > 0
        return FakeResponse(CONTENT, url="https://example.invalid/redirected.whl")

    status = CudaRuntimeManager(tmp_path, manifest=manifest, opener=opener).install()

    assert status.state == "failed"
    assert "redirect" in status.error
    assert not (tmp_path / ".staging" / "test-v1" / "wheels" / manifest.wheels[0].filename).exists()


def test_network_failure_discards_unverified_partial_archive(tmp_path: Path) -> None:
    manifest = mini_manifest()

    def opener(_request, timeout: float):
        assert timeout > 0
        raise URLError("offline")

    archive = tmp_path / ".staging" / "test-v1" / "wheels" / manifest.wheels[0].filename
    archive = archive.with_suffix(archive.suffix + ".part")
    archive.parent.mkdir(parents=True)
    archive.write_bytes(CONTENT[:10])

    status = CudaRuntimeManager(tmp_path, manifest=manifest, opener=opener).install()

    assert status.state == "failed"
    assert not archive.exists()
    assert not (tmp_path / "test-v1").exists()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(wheel_bytes(("../outside.py", b"bad")), id="traversal"),
        pytest.param(wheel_bytes(("/absolute.py", b"bad")), id="absolute"),
        pytest.param(
            wheel_bytes(
                (lambda info: (setattr(info, "external_attr", 0o120777 << 16), info)[1])(
                    zipfile.ZipInfo("demo/link")
                )
            ),
            id="symlink",
        ),
        pytest.param(
            wheel_bytes(("node", b"file"), ("node/child.py", b"bad")),
            id="file-directory-conflict",
        ),
    ],
)
def test_unsafe_wheel_layout_never_creates_active_runtime(tmp_path: Path, content: bytes) -> None:
    manager = CudaRuntimeManager(
        tmp_path,
        manifest=mini_manifest(content),
        downloader=write_wheel(content),
    )

    status = manager.install()

    assert status.state == "failed"
    assert "unsafe wheel member" in status.error
    assert not (tmp_path / "test-v1").exists()
    assert not (
        tmp_path / ".staging" / "test-v1" / "wheels" / "demo-1.0-py3-none-any.whl.part"
    ).exists()


def test_conflicting_wheel_outputs_never_overwrite_prior_package_files(tmp_path: Path) -> None:
    first = wheel_bytes(("demo/item.py", b"first"))
    second = wheel_bytes(("demo/item.py", b"second"))
    wheels = tuple(
        RuntimeWheel(
            f"demo-{index}-py3-none-any.whl",
            f"https://files.pythonhosted.org/packages/a/demo-{index}-py3-none-any.whl",
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
        for index, content in enumerate((first, second), start=1)
    )
    manifest = CudaRuntimeManifest("test-v1", "linux-x64", "cp313", wheels)

    def downloader(item: RuntimeWheel, target: Path) -> None:
        target.write_bytes((first, second)[wheels.index(item)])

    status = CudaRuntimeManager(tmp_path, manifest=manifest, downloader=downloader).install()

    assert status.state == "failed"
    assert "unsafe wheel member" in status.error
    assert not (tmp_path / "test-v1").exists()


def test_existing_staging_symlink_cannot_redirect_extraction(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    staging_site_packages = tmp_path / ".staging" / "test-v1" / "site-packages"
    staging_site_packages.parent.mkdir(parents=True)
    staging_site_packages.symlink_to(external, target_is_directory=True)
    manager = CudaRuntimeManager(
        tmp_path,
        manifest=mini_manifest(),
        downloader=write_wheel(CONTENT),
    )

    status = manager.install()

    assert status.state == "ready"
    assert not (external / "demo" / "__init__.py").exists()
    assert status.location is not None
    assert (status.location.site_packages / "demo" / "__init__.py").is_file()


@pytest.mark.parametrize("symlink_part", [".staging", "format", "wheels"])
def test_symlinked_staging_ancestors_fail_without_downloading_or_promotion(
    tmp_path: Path,
    symlink_part: str,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    staging = tmp_path / ".staging" / "test-v1"
    if symlink_part == ".staging":
        staging.parent.symlink_to(external, target_is_directory=True)
    elif symlink_part == "format":
        staging.parent.mkdir()
        staging.symlink_to(external, target_is_directory=True)
    else:
        staging.mkdir(parents=True)
        (staging / "wheels").symlink_to(external, target_is_directory=True)
    calls: list[RuntimeWheel] = []

    def downloader(item: RuntimeWheel, target: Path) -> None:
        calls.append(item)
        target.write_bytes(CONTENT)

    status = CudaRuntimeManager(
        tmp_path,
        manifest=mini_manifest(),
        downloader=downloader,
    ).install()

    assert status.state == "failed"
    assert "staging" in status.error
    assert calls == []
    assert not (tmp_path / "test-v1").exists()
    assert not list(external.iterdir())


def test_inspect_rejects_symlinked_active_runtime(tmp_path: Path) -> None:
    manager = CudaRuntimeManager(
        tmp_path,
        manifest=mini_manifest(),
        downloader=write_wheel(CONTENT),
    )
    assert manager.install().state == "ready"
    active = tmp_path / "test-v1"
    external = tmp_path / "external"
    active.replace(external)
    active.symlink_to(external, target_is_directory=True)

    status = manager.inspect()

    assert status.state == "failed"
    assert status.location is None
    assert "symlink" in status.error


def test_inspect_reports_corrupt_metadata_as_failed(tmp_path: Path) -> None:
    active = tmp_path / "test-v1"
    active.mkdir()
    (active / "install.json").write_text("{not json", encoding="utf-8")

    status = CudaRuntimeManager(tmp_path, manifest=mini_manifest()).inspect()

    assert status.state == "failed"
    assert status.location is None
    assert "metadata" in status.error


def test_install_metadata_is_path_free_and_has_only_manifest_identifiers(tmp_path: Path) -> None:
    manager = CudaRuntimeManager(
        tmp_path, manifest=mini_manifest(), downloader=write_wheel(CONTENT)
    )

    status = manager.install()

    assert status.location is not None
    metadata = json.loads((status.location.root / "install.json").read_text(encoding="utf-8"))
    assert set(metadata) == {"format", "platform", "python_tag", "wheels"}
    assert metadata["wheels"] == {"demo-1.0-py3-none-any.whl": mini_manifest().wheels[0].sha256}
    assert str(tmp_path) not in json.dumps(metadata)


def test_failed_promotion_restores_previous_verified_runtime(tmp_path: Path, monkeypatch) -> None:
    manager = CudaRuntimeManager(
        tmp_path, manifest=mini_manifest(), downloader=write_wheel(CONTENT)
    )
    initial = manager.install()
    assert initial.state == "ready"
    staging = tmp_path / ".staging" / "test-v1"
    (staging / "site-packages" / "demo").mkdir(parents=True)
    (staging / "site-packages" / "demo" / "__init__.py").write_bytes(b"")
    (staging / "install.json").write_text(
        json.dumps(
            {
                "format": "test-v1",
                "platform": "linux-x64",
                "python_tag": "cp313",
                "wheels": {"demo-1.0-py3-none-any.whl": mini_manifest().wheels[0].sha256},
            }
        ),
        encoding="utf-8",
    )
    real_replace = __import__("os").replace

    def fail_staging_promotion(source, target):
        if Path(source) == staging and Path(target) == tmp_path / "test-v1":
            raise OSError("simulated promotion failure")
        return real_replace(source, target)

    monkeypatch.setattr("vienetts_app.core.cuda_runtime.os.replace", fail_staging_promotion)

    status = manager._promote_staging()

    assert status.state == "failed"
    assert manager.inspect().state == "ready"
    assert (tmp_path / "test-v1").is_dir()


def test_remove_refuses_loaded_runtime_and_cancel_staging_removes_only_staging(
    tmp_path: Path,
) -> None:
    manager = CudaRuntimeManager(
        tmp_path, manifest=mini_manifest(), downloader=write_wheel(CONTENT)
    )
    assert manager.install().state == "ready"
    staging_file = tmp_path / ".staging" / "test-v1" / "wheels" / "partial.whl.part"
    staging_file.parent.mkdir(parents=True)
    staging_file.write_bytes(b"partial")

    refused = manager.remove(in_use=True)
    manager.cancel_staging()

    assert refused.state == "failed"
    assert "in use" in refused.error
    assert manager.inspect().state == "ready"
    assert not staging_file.exists()
    assert manager.remove().state == "unavailable"
    assert manager.inspect().state == "unavailable"


def test_cancel_staging_unlinks_a_staging_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    staging = tmp_path / ".staging" / "test-v1"
    staging.parent.mkdir(parents=True)
    staging.symlink_to(external, target_is_directory=True)

    CudaRuntimeManager(tmp_path, manifest=mini_manifest()).cancel_staging()

    assert not staging.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
