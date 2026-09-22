"""Qwen runtime installer lifecycle, resume, repair, and security tests."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from vienetts_app.core.qwen_runtime import (
    QwenRuntimeManager,
    _running_python_tag,
)
from vienetts_app.core.qwen_runtime_manifest import QwenRuntimeManifest

RUNNING_TAG = _running_python_tag()


def wheel_bytes(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member in members:
            archive.writestr(*member)
    return buffer.getvalue()


TORCH_CONTENT = wheel_bytes(("torch/__init__.py", b""))
DEMO_CONTENT = wheel_bytes(("demo/__init__.py", b""))


def _wheel(filename: str, content: bytes, host: str = "files.pythonhosted.org"):
    from vienetts_app.core.managed_install import RuntimeWheel

    prefix = "/whl/cpu/" if "pytorch.org" in host else "/packages/a/"
    return RuntimeWheel(
        filename,
        f"https://{host}{prefix}{filename.replace('+', '%2B')}",
        len(content),
        hashlib.sha256(content).hexdigest(),
    )


def mini_manifest(
    *,
    torch_content: bytes = TORCH_CONTENT,
    demo_content: bytes = DEMO_CONTENT,
    python_tag: str = RUNNING_TAG,
    torch_url_host: str = "download.pytorch.org",
) -> QwenRuntimeManifest:

    torch_wheel = _wheel(
        f"torch-2.8.0+cpu-{RUNNING_TAG}-{RUNNING_TAG}-manylinux_2_28_x86_64.whl",
        torch_content,
        host=torch_url_host,
    )
    demo_wheel = _wheel("demo-1.0-py3-none-any.whl", demo_content)
    return QwenRuntimeManifest(
        format_version="qwen-runtime-v1",
        platform_key="linux-x64-cpu",
        platform_tag="linux_x86_64",
        device="cpu",
        python_tag=python_tag,
        torch_local_version="2.8.0+cpu",
        pins={
            "qwen-tts": "0.1.1",
            "transformers": "4.57.3",
            "torch": "2.8.0+cpu",
            "torchaudio": "2.8.0+cpu",
        },
        wheels=(torch_wheel, demo_wheel),
    )


def contents_for(manifest: QwenRuntimeManifest) -> dict[str, bytes]:
    return {
        manifest.wheels[0].filename: TORCH_CONTENT,
        manifest.wheels[1].filename: DEMO_CONTENT,
    }


def pack_downloader(payload: dict[str, bytes], calls: list[str] | None = None):
    def download(wheel, target: Path) -> None:
        if calls is not None:
            calls.append(wheel.filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload[wheel.filename])

    return download


def imports_ok(_site_packages: Path) -> tuple[bool, str]:
    """Stand-in for the host import check: these runtimes are synthetic zips."""
    return True, ""


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        self._buffer = io.BytesIO(content)
        self.status = status
        self.headers = headers or {}
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


def test_install_promotes_only_a_verified_runtime(tmp_path: Path) -> None:
    manifest = mini_manifest()
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest)),
        import_check=imports_ok,
    )

    status = manager.install()

    assert status.state == "ready"
    assert status.location is not None
    assert (status.location.root / "install.json").is_file()
    assert (status.location.site_packages / "torch" / "__init__.py").is_file()
    assert (status.location.site_packages / "demo" / "__init__.py").is_file()
    assert not (tmp_path / ".staging" / manifest.format_version).exists()
    assert status.progress == 1.0


def test_url_policy_rejects_a_wheel_outside_the_allowlist(tmp_path: Path) -> None:
    manifest = mini_manifest(torch_url_host="example.invalid")
    with pytest.raises(ValueError, match="host is unsupported"):
        QwenRuntimeManager(tmp_path, manifest)


def test_checksum_failure_never_creates_active_runtime(tmp_path: Path) -> None:
    manifest = mini_manifest()
    payload = contents_for(manifest)
    payload[manifest.wheels[0].filename] = b"not the pinned bytes"
    manager = QwenRuntimeManager(tmp_path, manifest, downloader=pack_downloader(payload))

    status = manager.install()

    assert status.state == "failed"
    assert "checksum mismatch" in status.error
    assert status.location is None
    assert not list(tmp_path.glob("*/install.json"))
    wheels = tmp_path / ".staging" / manifest.format_version / "wheels"
    assert not list(wheels.glob("*.part")) if wheels.is_dir() else True


def test_corrupt_wheel_archive_fails_without_promoting(tmp_path: Path) -> None:
    corrupt = b"x" * 128  # valid size and digest, but not a zip archive
    manifest = mini_manifest(torch_content=corrupt)
    payload = contents_for(manifest)
    payload[manifest.wheels[0].filename] = corrupt
    manager = QwenRuntimeManager(tmp_path, manifest, downloader=pack_downloader(payload))

    status = manager.install()

    assert status.state == "failed"
    assert status.location is None
    assert manager.inspect().state == "unavailable"
    assert not list(tmp_path.glob("*/install.json"))


def test_interrupted_download_resumes_from_the_partial_archive(tmp_path: Path) -> None:
    manifest = mini_manifest()
    wheel = manifest.wheels[0]
    partial = TORCH_CONTENT[: len(TORCH_CONTENT) // 2]
    archive = tmp_path / ".staging" / manifest.format_version / "wheels" / f"{wheel.filename}.part"
    requests: list = []

    def opener(request, timeout: float):
        requests.append(request)
        url = request.full_url
        assert url in {item.url for item in manifest.wheels}
        if url == wheel.url:
            return FakeResponse(
                TORCH_CONTENT[len(partial) :],
                status=206,
                headers={
                    "Content-Range": f"bytes {len(partial)}-{len(TORCH_CONTENT) - 1}"
                    f"/{len(TORCH_CONTENT)}"
                },
                url=url,
            )
        return FakeResponse(DEMO_CONTENT, url=url)

    manager = QwenRuntimeManager(tmp_path, manifest, opener=opener, import_check=imports_ok)
    archive.parent.mkdir(parents=True)
    archive.write_bytes(partial)

    status = manager.install()

    assert status.state == "ready"
    assert requests[0].get_header("Range") == f"bytes={len(partial)}-"
    assert status.location is not None
    assert (status.location.site_packages / "torch" / "__init__.py").is_file()
    assert (status.location.site_packages / "demo" / "__init__.py").is_file()


def test_cancellation_keeps_partial_archive_and_never_promotes(tmp_path: Path) -> None:
    manifest = mini_manifest()
    archive = (
        tmp_path
        / ".staging"
        / manifest.format_version
        / "wheels"
        / f"{manifest.wheels[0].filename}.part"
    )

    def opener(request, timeout: float):
        return FakeResponse(
            TORCH_CONTENT[: len(TORCH_CONTENT) // 2],
            url=manifest.wheels[0].url,
        )

    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        opener=opener,
        downloader=lambda item, target: target.write_bytes(contents_for(manifest)[item.filename]),
    )
    calls = {"count": 0}

    def cancelled() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    status = manager.install(cancelled)

    assert status.state == "unavailable"
    assert manager.inspect().state == "unavailable"
    assert not (tmp_path / manifest.format_version).exists()
    assert not list(tmp_path.glob("*/install.json"))
    # The interrupted wheel stays on disk so the next install can resume it.
    assert archive.is_file()


def test_repair_keeps_valid_archives_and_redownloads_invalid_ones(tmp_path: Path) -> None:
    manifest = mini_manifest()
    calls: list[str] = []
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest), calls),
        import_check=imports_ok,
    )
    wheels_dir = tmp_path / ".staging" / manifest.format_version / "wheels"
    wheels_dir.mkdir(parents=True)
    (wheels_dir / f"{manifest.wheels[0].filename}.part").write_bytes(TORCH_CONTENT)
    (wheels_dir / f"{manifest.wheels[1].filename}.part").write_bytes(b"stale")

    status = manager.repair()

    assert status.state == "ready"
    assert calls == [manifest.wheels[1].filename]


def test_manifest_drift_is_reported_as_failed(tmp_path: Path) -> None:
    manifest = mini_manifest()
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest)),
        import_check=imports_ok,
    )
    status = manager.install()
    assert status.state == "ready"
    assert status.location is not None

    (status.location.root / "install.json").write_text(
        json.dumps(
            {
                "format": manifest.format_version,
                "platform": manifest.platform_key,
                "python_tag": manifest.python_tag,
                "wheels": {manifest.wheels[0].filename: "0" * 64},
            }
        ),
        encoding="utf-8",
    )

    drifted = manager.inspect()

    assert drifted.state == "failed"
    assert "does not match the manifest" in drifted.error


def test_python_tag_mismatch_and_low_disk_refuse_before_downloading(tmp_path: Path) -> None:
    calls: list[str] = []
    mismatched = mini_manifest(python_tag="cp37")
    tag_manager = QwenRuntimeManager(
        tmp_path, mismatched, downloader=pack_downloader(contents_for(mismatched), calls)
    )

    tag_status = tag_manager.install()

    assert tag_status.state == "failed"
    assert "targets Python cp37" in tag_status.error
    assert calls == []

    manifest = mini_manifest()
    space_manager = QwenRuntimeManager(
        tmp_path / "space",
        manifest,
        downloader=pack_downloader(contents_for(manifest), calls),
        disk_usage=lambda _path: type("Usage", (), {"free": 1})(),
    )

    space_status = space_manager.install()

    assert space_status.state == "failed"
    assert "insufficient disk space" in space_status.error
    assert calls == []


def test_rollback_keeps_previous_runtime_after_failed_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = mini_manifest()
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest)),
        import_check=imports_ok,
    )
    assert manager.install().state == "ready"
    staging = tmp_path / ".staging" / manifest.format_version
    (staging / "site-packages" / "demo").mkdir(parents=True)
    (staging / "install.json").write_text(json.dumps(manager._metadata()), encoding="utf-8")
    real_replace = __import__("os").replace

    def fail_promotion(source, target):
        if Path(source) == staging and Path(target) == tmp_path / manifest.format_version:
            raise OSError("simulated promotion failure")
        return real_replace(source, target)

    monkeypatch.setattr("vienetts_app.core.qwen_runtime.os.replace", fail_promotion)

    status = manager._promote_staging()

    assert status.state == "failed"
    assert manager.inspect().state == "ready"
    assert (tmp_path / manifest.format_version).is_dir()


def test_remove_refuses_in_use_and_cancel_staging_removes_only_staging(tmp_path: Path) -> None:
    manifest = mini_manifest()
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest)),
        import_check=imports_ok,
    )
    assert manager.install().state == "ready"
    staging_file = tmp_path / ".staging" / manifest.format_version / "wheels" / "partial.whl.part"
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


def test_offline_pack_install_verifies_every_file(tmp_path: Path) -> None:
    manifest = mini_manifest()
    pack = tmp_path / "pack"
    pack.mkdir()
    for filename, content in contents_for(manifest).items():
        (pack / filename).write_bytes(content)

    offline = tmp_path / "offline"
    offline_manager = QwenRuntimeManager(offline, manifest, import_check=imports_ok)
    status = offline_manager.install_from_offline_pack(pack)

    assert status.state == "ready"
    assert (status.location.site_packages / "demo" / "__init__.py").is_file()

    incomplete = tmp_path / "incomplete"
    (pack / manifest.wheels[1].filename).unlink()
    missing = QwenRuntimeManager(incomplete, manifest).install_from_offline_pack(pack)

    assert missing.state == "failed"
    assert "offline pack is missing" in missing.error
    assert not list(incomplete.glob("*/install.json"))

    corrupted = tmp_path / "corrupted"
    (pack / manifest.wheels[1].filename).write_bytes(b"tampered")
    tampered = QwenRuntimeManager(corrupted, manifest).install_from_offline_pack(pack)

    assert tampered.state == "failed"
    assert "does not match the manifest" in tampered.error
    assert not list(corrupted.glob("*/install.json"))


# --------------------------------------------------------------------------- #
# import gate: a pinned closure that installs but cannot import
# --------------------------------------------------------------------------- #


def _failing_imports(_site_packages: Path) -> tuple[bool, str]:
    return False, "the managed Qwen runtime is incomplete: Python module 'sox' is missing"


def test_an_install_that_cannot_import_is_rejected(tmp_path: Path) -> None:
    """The sox defect: a closure whose modules are missing must never go live.

    Every archive matched the manifest, so extraction and promotion both
    succeeded; only the host's own import proved the runtime unusable.
    """
    manifest = mini_manifest()
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest)),
        import_check=_failing_imports,
    )

    status = manager.install()

    assert status.state == "failed"
    assert "'sox' is missing" in status.error
    assert "linux-x64-cpu" in status.error  # the report needs the platform
    assert status.location is None
    assert manager.inspect().state == "unavailable"
    assert not list(tmp_path.glob("*/install.json"))


def test_an_import_failure_keeps_the_previous_runtime(tmp_path: Path) -> None:
    """A repair that would break a working runtime rolls back instead."""
    manifest = mini_manifest()
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest)),
        import_check=imports_ok,
    )
    assert manager.install().state == "ready"
    staging = tmp_path / ".staging" / manifest.format_version
    (staging / "wheels").mkdir(parents=True)
    (staging / "site-packages" / "demo").mkdir(parents=True)
    (staging / "install.json").write_text(json.dumps(manager._metadata()), encoding="utf-8")

    failing = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest)),
        import_check=_failing_imports,
    )
    status = failing._promote_staging()

    assert status.state == "failed"
    assert "'sox' is missing" in status.error
    assert failing.inspect().state == "ready"  # the previous install survived
    assert (tmp_path / manifest.format_version).is_dir()


def test_repair_reinstalls_a_runtime_that_cannot_import(tmp_path: Path) -> None:
    """Repair must not hand back the same broken runtime as "ready".

    The metadata of an install that fails to import still matches the manifest,
    so ``inspect()`` alone would make Repair a no-op and leave the user in the
    failure loop the message just described.
    """
    manifest = mini_manifest()
    calls: list[str] = []
    healthy = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest), calls),
        import_check=imports_ok,
    )
    assert healthy.install().state == "ready"
    calls.clear()

    broken = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest), calls),
        import_check=_failing_imports,
    )
    status = broken.repair()

    assert status.state == "failed"
    assert "'sox' is missing" in status.error
    # It really reinstalled: every wheel was fetched again, and the broken
    # closure was rejected at the gate instead of promoted.
    assert calls == [wheel.filename for wheel in manifest.wheels]
    assert broken.inspect().state == "ready"  # the previous install survived


def test_repair_leaves_a_healthy_runtime_alone(tmp_path: Path) -> None:
    """A repair that finds a working runtime must not re-download it."""
    manifest = mini_manifest()
    calls: list[str] = []
    manager = QwenRuntimeManager(
        tmp_path,
        manifest,
        downloader=pack_downloader(contents_for(manifest), calls),
        import_check=imports_ok,
    )
    assert manager.install().state == "ready"
    calls.clear()

    status = manager.repair()

    assert status.state == "ready"
    assert calls == []


def test_the_import_check_runs_the_host_and_names_the_missing_module(tmp_path: Path) -> None:
    """The real check: the host interpreter imports the runtime and reports.

    No torch anywhere in this process — the verdict comes from a child started
    exactly the way a load is started, which is the only place the promoted
    closure can be exercised.
    """
    from vienetts_app.core.qwen_runtime import check_runtime_imports

    site_packages = tmp_path / "site-packages"
    (site_packages / "torch").mkdir(parents=True)
    (site_packages / "torch" / "__init__.py").write_text("", encoding="utf-8")
    (site_packages / "qwen_tts").mkdir()
    (site_packages / "qwen_tts" / "__init__.py").write_text(
        "import vienetts_missing_module_probe\n", encoding="utf-8"
    )

    ok, detail = check_runtime_imports(site_packages)

    assert ok is False
    assert "vienetts_missing_module_probe" in detail
