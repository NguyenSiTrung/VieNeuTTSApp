"""Shared managed-install primitives: verification, promotion, download policy.

These primitives are shared by the VieNeu model installer, the managed CUDA
runtime and (Phase 2) the Qwen runtime/model managers, so their edge cases are
tested once: truncation, corruption, redirect rejection, cancellation,
atomic-failure rollback and Windows-locked removal.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from vienetts_app.core import managed_install as mi


def write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class TestVerification:
    def test_verification_contract(self, tmp_path: Path, monkeypatch) -> None:
        target = write(tmp_path / "blob.bin", b"x" * (mi.CHUNK_SIZE + 17))
        assert mi.sha256_of(target) == hashlib.sha256(target.read_bytes()).hexdigest()

        payload = b"payload" * 100
        digest = hashlib.sha256(payload).hexdigest()
        good = write(tmp_path / "good.bin", payload)
        assert mi.file_matches(good, len(payload), digest) is True

        truncated = write(tmp_path / "short.bin", payload[:-1])
        assert mi.file_matches(truncated, len(payload), digest) is False

        corrupt = write(tmp_path / "corrupt.bin", b"z" * len(payload))
        assert mi.file_matches(corrupt, len(payload), digest) is False

        assert mi.file_matches(tmp_path / "missing.bin", len(payload), digest) is False

        target = write(tmp_path / "blob.bin", b"data")
        monkeypatch.setattr(mi, "sha256_of", lambda _path: (_ for _ in ()).throw(OSError("locked")))
        assert mi.file_matches(target, 4, "0" * 64) is False


class TestStampedVerification:
    """Stat stamps let an unchanged verified file skip its full SHA-256."""

    @staticmethod
    def _counting_hash(monkeypatch) -> list[Path]:
        calls: list[Path] = []
        real = mi.sha256_of

        def spy(path: Path) -> str:
            calls.append(path)
            return real(path)

        monkeypatch.setattr(mi, "sha256_of", spy)
        return calls

    def test_file_stamp_is_size_mtime_and_inode(self, tmp_path: Path) -> None:
        target = write(tmp_path / "blob.bin", b"abc")
        info = target.stat()
        assert mi.file_stamp(target) == (3, info.st_mtime_ns, info.st_ino)
        assert mi.file_stamp(tmp_path / "missing.bin") is None

    def test_a_missing_stamp_hashes_and_returns_the_fresh_stamp(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        payload = b"payload" * 100
        good = write(tmp_path / "good.bin", payload)
        calls = self._counting_hash(monkeypatch)

        stamp = mi.file_matches_stamped(good, len(payload), hashlib.sha256(payload).hexdigest())

        assert stamp == mi.file_stamp(good)
        assert calls == [good]

    def test_an_equal_stamp_skips_the_hash(self, tmp_path: Path, monkeypatch) -> None:
        payload = b"payload" * 100
        good = write(tmp_path / "good.bin", payload)
        stamp = mi.file_stamp(good)
        calls = self._counting_hash(monkeypatch)

        # The digest is deliberately wrong: an equal stamp trusts the
        # earlier verification and never reads the file.
        assert mi.file_matches_stamped(good, len(payload), "0" * 64, stamp) == stamp
        # A stamp that round-tripped through JSON arrives as a list.
        assert mi.file_matches_stamped(good, len(payload), "0" * 64, list(stamp)) == stamp
        assert calls == []

    def test_a_changed_file_is_hashed_and_corruption_still_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        payload = b"payload" * 100
        digest = hashlib.sha256(payload).hexdigest()
        target = write(tmp_path / "good.bin", payload)
        stamp = mi.file_stamp(target)
        target.write_bytes(b"z" * len(payload))
        size, mtime_ns, inode = stamp
        os.utime(target, ns=(mtime_ns + 1_000_000, mtime_ns + 1_000_000))
        calls = self._counting_hash(monkeypatch)

        assert mi.file_matches_stamped(target, len(payload), digest, stamp) is None
        assert calls == [target]

    def test_a_stamp_for_another_size_or_a_missing_file_never_matches(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        payload = b"payload" * 100
        target = write(tmp_path / "good.bin", payload)
        stamp = mi.file_stamp(target)
        calls = self._counting_hash(monkeypatch)

        assert mi.file_matches_stamped(target, len(payload) + 1, "0" * 64, stamp) is None
        assert mi.file_matches_stamped(tmp_path / "gone.bin", len(payload), "0" * 64, stamp) is None
        assert calls == []

    def test_malformed_stamps_fall_back_to_a_hash(self, tmp_path: Path, monkeypatch) -> None:
        payload = b"payload"
        digest = hashlib.sha256(payload).hexdigest()
        target = write(tmp_path / "good.bin", payload)
        calls = self._counting_hash(monkeypatch)

        for bad in ("nope", [1, 2], {"size": 7}, [None, None, None], 42):
            assert mi.file_matches_stamped(target, len(payload), digest, bad) == mi.file_stamp(
                target
            )
        assert len(calls) == 5

    def test_a_zero_inode_compares_size_and_mtime_only(self, tmp_path: Path, monkeypatch) -> None:
        """Windows filesystems may report st_ino 0: size + mtime decide."""
        payload = b"payload"
        target = write(tmp_path / "good.bin", payload)
        size, mtime_ns, _inode = mi.file_stamp(target)
        calls = self._counting_hash(monkeypatch)

        assert mi.file_matches_stamped(target, size, "0" * 64, (size, mtime_ns, 0)) is not None
        monkeypatch.setattr(mi, "file_stamp", lambda _p: (size, mtime_ns, 0))
        assert mi.file_matches_stamped(target, size, "0" * 64, (size, mtime_ns, 99)) is not None
        assert calls == []
        # The inode still distinguishes a replaced file when both sides know it.
        monkeypatch.setattr(mi, "file_stamp", lambda _p: (size, mtime_ns, 7))
        assert mi.file_matches_stamped(target, size, "0" * 64, (size, mtime_ns, 99)) is None
        assert len(calls) == 1


class TestStampLedger:
    def test_split_install_record_separates_stamps(self) -> None:
        record, stamps = mi.split_install_record({"format": "v1", "stamps": {"a": [1, 2, 3]}})
        assert record == {"format": "v1"}
        assert stamps == {"a": [1, 2, 3]}
        assert mi.split_install_record({"format": "v1", "stamps": "junk"}) == ({"format": "v1"}, {})
        assert mi.split_install_record(["not", "a", "dict"]) == (["not", "a", "dict"], {})

    def test_persist_writes_fresh_stamps_once(self, tmp_path: Path) -> None:
        payload = b"payload"
        digest = hashlib.sha256(payload).hexdigest()
        target = write(tmp_path / "blob.bin", payload)
        install = tmp_path / "install.json"
        install.write_text('{"format": "v1"}', encoding="utf-8")

        ledger = mi.StampLedger({})
        assert ledger.matches("files/blob.bin", target, len(payload), digest)
        ledger.persist(install, {"format": "v1"})

        written = json.loads(install.read_text(encoding="utf-8"))
        assert written == {
            "format": "v1",
            "stamps": {"files/blob.bin": list(mi.file_stamp(target))},
        }
        assert not (tmp_path / "install.json.tmp").exists()
        before = install.stat().st_mtime_ns
        again = mi.StampLedger(written["stamps"])
        assert again.matches("files/blob.bin", target, len(payload), digest)
        again.persist(install, {"format": "v1"})
        assert install.stat().st_mtime_ns == before  # unchanged stamps: no rewrite

    def test_full_mode_ignores_recorded_stamps(self, tmp_path: Path, monkeypatch) -> None:
        payload = b"payload"
        target = write(tmp_path / "blob.bin", payload)
        stamps = {"k": list(mi.file_stamp(target))}
        calls: list[Path] = []
        real = mi.sha256_of
        monkeypatch.setattr(mi, "sha256_of", lambda p: calls.append(p) or real(p))

        ledger = mi.StampLedger(stamps, mode="full")
        assert ledger.matches("k", target, len(payload), hashlib.sha256(payload).hexdigest())
        assert calls == [target]
        with pytest.raises(ValueError, match="verification mode"):
            mi.StampLedger({}, mode="quick")


class TestPathHandling:
    def test_path_handling(self, tmp_path: Path) -> None:
        assert mi.normalize_windows_path("\\\\?\\C:\\models\\x") == "C:\\models\\x"
        assert mi.normalize_windows_path("\\\\?\\UNC\\server\\share") == "\\\\server\\share"
        assert mi.normalize_windows_path("C:/models/x") == "C:\\models\\x"

        target = write(tmp_path / "a.bin", b"data")
        assert mi.is_same_file(target, target) is True
        assert mi.is_same_file(target, tmp_path / "." / "a.bin") is True
        assert mi.is_same_file(target, tmp_path / "b.bin") is False


class TestFreeSpace:
    def test_free_space(self, tmp_path: Path) -> None:
        class Usage:
            free = 123

        assert mi.free_space_bytes(tmp_path, lambda _path: Usage()) == 123

        def broken(_path):
            raise OSError("no such volume")

        assert mi.free_space_bytes(tmp_path, broken) is None
        assert mi.free_space_bytes(tmp_path, lambda _path: object()) is None


class TestSafeRemove:
    def test_safe_remove(self, tmp_path: Path, monkeypatch) -> None:
        target = write(tmp_path / "tree" / "file.bin", b"data")
        mi.safe_remove(target)
        assert not target.exists()

        mi.safe_remove(tmp_path / "tree")
        assert not (tmp_path / "tree").exists()

        link = tmp_path / "link"
        link.symlink_to(tmp_path / "gone")
        mi.safe_remove(link)
        assert not link.exists()

        mi.safe_remove(tmp_path / "never-existed")  # no-op

        target = write(tmp_path / "tree" / "file.bin", b"data")
        calls = {"count": 0}
        real_rmtree = mi.shutil.rmtree

        def flaky(path, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                raise OSError(32, "in use by another process")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(mi.shutil, "rmtree", flaky)
        monkeypatch.setattr(mi.time, "sleep", lambda _seconds: None)
        mi.safe_remove(tmp_path / "tree")
        assert calls["count"] == 3
        assert not (tmp_path / "tree").exists()

        monkeypatch.setattr(
            mi.shutil,
            "rmtree",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError(32, "locked forever")),
        )
        mi.safe_remove(target)  # gives up silently after REMOVAL_ATTEMPTS


class TestPromotion:
    def test_promotion_contract(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        write(staging / "new.bin", b"new")
        write(tmp_path / "active" / "old.bin", b"old")
        active = tmp_path / "active"
        previous = tmp_path / "active.previous"

        with mi.promoted_install(staging, active, previous):
            assert (active / "new.bin").is_file()
            assert not staging.exists()

        assert not previous.exists()
        assert (active / "new.bin").is_file()

        staging = tmp_path / "staging"
        write(staging / "new.bin", b"new")
        write(tmp_path / "active" / "broken.bin", b"broken")
        active = tmp_path / "active"
        previous = tmp_path / "active.previous"

        with mi.promoted_install(staging, active, previous, active_usable=lambda: False):
            assert (active / "new.bin").is_file()

        assert not previous.exists()

    def test_promotion_failures(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        write(staging / "new.bin", b"new")
        write(tmp_path / "active" / "old.bin", b"old")
        active = tmp_path / "active"
        previous = tmp_path / "active.previous"

        with (
            pytest.raises(mi.InstallPromotionError, match="verification failed"),
            mi.promoted_install(staging, active, previous),
        ):
            assert (active / "new.bin").is_file()
            raise mi.InstallPromotionError("verification failed")

        assert (active / "old.bin").is_file()
        assert not (active / "new.bin").exists()
        assert not previous.exists()

        staging = tmp_path / "staging-b"
        write(staging / "new.bin", b"new")
        active = tmp_path / "active-b"
        previous = tmp_path / "active-b.previous"

        with (
            pytest.raises(mi.InstallPromotionError),
            mi.promoted_install(staging, active, previous),
        ):
            raise mi.InstallPromotionError("staging was invalid")

        assert not active.exists()

        write(tmp_path / "active" / "old.bin", b"old")
        active = tmp_path / "active"
        previous = tmp_path / "active.previous"

        with (
            pytest.raises(mi.InstallPromotionError, match="promotion failed"),
            mi.promoted_install(tmp_path / "missing-staging", active, previous),
        ):
            pass

        assert (active / "old.bin").is_file()


class TestDownloadPolicy:
    def test_download_policy(self) -> None:
        mi.check_download_url(
            "https://files.pythonhosted.org/packages/aa/bb/cc/torch-2.8.0-cp312-cp312-win_amd64.whl",
            filename="torch-2.8.0-cp312-cp312-win_amd64.whl",
            required_path_prefixes=("/packages/",),
        )
        mi.check_download_url(
            "https://download.pytorch.org/whl/cu128/torch-2.8.0%2Bcu128-cp312-cp312-win_amd64.whl",
            required_path_prefixes=("/whl/cu128/",),
        )

        with pytest.raises(ValueError, match="HTTPS"):
            mi.check_download_url("http://files.pythonhosted.org/packages/x/y.whl")
        with pytest.raises(ValueError, match="host is unsupported"):
            mi.check_download_url("https://evil.example.com/torch-2.8.0.whl")
        with pytest.raises(ValueError, match="direct artifact"):
            mi.check_download_url("https://files.pythonhosted.org/packages/x.whl?token=1")
        with pytest.raises(ValueError, match="must point at"):
            mi.check_download_url(
                "https://files.pythonhosted.org/packages/other.whl", filename="torch.whl"
            )
        with pytest.raises(ValueError, match="pinned artifact"):
            mi.check_download_url(
                "https://download.pytorch.org/whl/cpu/torch-2.8.0.whl",
                required_path_prefixes=("/whl/cu128/",),
            )


class TestResponseHelpers:
    class Response:
        def __init__(self, status: int, url: str, headers: dict[str, str]) -> None:
            self.status = status
            self.url = url
            self.headers = headers

    def test_response_metadata(self) -> None:
        response = self.Response(206, "https://x/y.whl", {"Content-Range": "bytes 10-99/100"})
        assert mi.response_status(response) == 206
        assert mi.response_url(response) == "https://x/y.whl"
        assert mi.response_header(response, "Content-Range") == "bytes 10-99/100"
        assert mi.response_header(response, "Missing") is None

        class Bare:
            def getcode(self) -> int:
                return 200

            def geturl(self) -> str:
                return "https://x/z.whl"

        assert mi.response_status(Bare()) == 200
        assert mi.response_url(Bare()) == "https://x/z.whl"
        assert mi.response_status(object()) == 0
        assert mi.response_url(object()) is None

        url = "https://x/y.whl"
        good = self.Response(206, url, {"Content-Range": "bytes 10-99/100"})
        assert mi.range_is_honored(good, offset=10, size_bytes=100, expected_url=url) is True
        assert (
            mi.range_is_honored(
                self.Response(206, url, {"Content-Range": "bytes 5-99/100"}),
                offset=10,
                size_bytes=100,
                expected_url=url,
            )
            is False
        )
        assert (
            mi.range_is_honored(
                self.Response(206, url, {"Content-Range": "bytes 10-99/200"}),
                offset=10,
                size_bytes=100,
                expected_url=url,
            )
            is False
        )
        assert (
            mi.range_is_honored(
                self.Response(200, url, {"Content-Range": "bytes 10-99/100"}),
                offset=10,
                size_bytes=100,
                expected_url=url,
            )
            is False
        )
        assert (
            mi.range_is_honored(
                self.Response(206, "https://x/other.whl", {"Content-Range": "bytes 10-99/100"}),
                offset=10,
                size_bytes=100,
                expected_url=url,
            )
            is False
        )
        assert (
            mi.range_is_honored(
                self.Response(206, url, {"Content-Range": "garbage"}),
                offset=10,
                size_bytes=100,
                expected_url=url,
            )
            is False
        )

    def test_streaming_and_resume(self, tmp_path: Path) -> None:
        class Body:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def read(self, size: int) -> bytes:
                chunk, self._payload = self._payload[:size], self._payload[size:]
                return chunk

        target = tmp_path / "blob.bin"
        mi.stream_to_file(
            Body(b"a" * 10), target, mode="wb", cancelled=lambda: False, maximum_bytes=10
        )
        assert target.read_bytes() == b"a" * 10

        with pytest.raises(mi.DownloadCancelled):
            mi.stream_to_file(
                Body(b"a" * 10),
                target,
                mode="wb",
                cancelled=lambda: True,
                maximum_bytes=10,
            )

        with pytest.raises(OSError, match="exceeds declared artifact size"):
            mi.stream_to_file(
                Body(b"a" * 11), target, mode="wb", cancelled=lambda: False, maximum_bytes=10
            )

        class Body:
            def read(self, size: int) -> bytes:
                return b""

        target = tmp_path / "blob.bin"
        target.write_bytes(b"head")
        mi.stream_to_file(
            Body(), target, mode="ab", cancelled=lambda: False, maximum_bytes=10, on_bytes=None
        )
        assert target.read_bytes() == b"head"


class TestArchiveMemberPolicy:
    def test_archive_policy(self) -> None:
        assert mi.is_safe_archive_member("pkg/module.py", 0o644) is True
        assert mi.is_safe_archive_member("pkg/sub/module.py", 0o644) is True
        assert mi.is_safe_archive_member("/etc/passwd", 0o644) is False
        assert mi.is_safe_archive_member("C:\\Windows\\system32", 0o644) is False
        assert mi.is_safe_archive_member("pkg/../../escape.py", 0o644) is False
        assert mi.is_safe_archive_member("", 0o644) is False
        assert mi.is_safe_archive_member("pkg/link", 0o120777) is False

        handler = mi.NoRedirectHandler()
        assert handler.redirect_request("req", None, 302, "Found", {}, "https://x") is None


def wheel_archive(*members: tuple[str, bytes]) -> bytes:
    """An in-memory wheel holding ``members`` in the given order."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member in members:
            archive.writestr(*member)
    return buffer.getvalue()


def demo_wheel(directory: str, count: int) -> bytes:
    """A wheel with ``count`` files under ``directory`` (pkg0/, pkg1/, ...)."""
    return wheel_archive(
        *((f"{directory}/pkg{index // 100}/mod{index}.py", b"x") for index in range(count))
    )


class TestWheelLayoutConflicts:
    """Cross-archive claims: nothing may be overwritten, however it is ordered.

    The managed Qwen runtime installs ~30k members from 87 wheels into one
    ``site-packages``, so the conflict check has to stay linear in the closure
    (VieNeuTTSApp-rn5: the old scan made it quadratic and the install never
    reached ``ready``).
    """

    def test_claim_conflicts(self, tmp_path: Path) -> None:
        site_packages = tmp_path / "site-packages"
        # The first member claims demo/child.py (so demo is a directory); a file
        # at demo would have to replace it.
        archive = tmp_path / "demo-1.0-py3-none-any.whl"
        archive.write_bytes(
            wheel_archive(("demo/child.py", b"child"), ("demo", b"not a directory"))
        )
        with pytest.raises(OSError, match="unsafe wheel member"):
            mi.extract_wheel_archive(archive.name, archive, site_packages, {})

        # Same across two wheels: the second wheel's file collides with the
        # directory the first wheel's file created.
        claimed: dict[Path, bool] = {}
        first = tmp_path / "first-1.0-py3-none-any.whl"
        first.write_bytes(wheel_archive(("demo/child.py", b"child")))
        mi.extract_wheel_archive(first.name, first, site_packages, claimed)
        second = tmp_path / "second-1.0-py3-none-any.whl"
        second.write_bytes(wheel_archive(("demo", b"file")))
        with pytest.raises(OSError, match="unsafe wheel member"):
            mi.extract_wheel_archive(second.name, second, site_packages, claimed)

        archive = tmp_path / "demo-1.0-py3-none-any.whl"
        archive.write_bytes(wheel_archive(("demo", b"file"), ("demo/child.py", b"child")))
        with pytest.raises(OSError, match="unsafe wheel member"):
            mi.extract_wheel_archive(archive.name, archive, tmp_path / "site-packages", {})

        claimed: dict[Path, bool] = {}
        archive = tmp_path / "demo-1.0-py3-none-any.whl"
        archive.write_bytes(wheel_archive(("demo/item.py", b"first")))
        mi.extract_wheel_archive(archive.name, archive, tmp_path / "site-packages", claimed)
        with pytest.raises(OSError, match="unsafe wheel member"):
            mi.extract_wheel_archive(archive.name, archive, tmp_path / "site-packages", claimed)

    def test_closure_sized_validation_stays_linear(self, tmp_path: Path) -> None:
        """Two 1,500-member wheels must validate in well under a second.

        The removed scan compared every member against every claim: 2.25M Path
        comparisons here (~40s on the 2026-09-21 machine, and hours for the real
        87-wheel closure). The budget is two orders of magnitude above the
        linear cost so a slow runner cannot fail it, and far below the
        quadratic one so a regression cannot pass it.
        """
        site_packages = tmp_path / "site-packages"
        first = tmp_path / "first-1.0-py3-none-any.whl"
        first.write_bytes(demo_wheel("demo", 1500))
        second = tmp_path / "second-1.0-py3-none-any.whl"
        second.write_bytes(demo_wheel("other", 1500))
        claimed: dict[Path, bool] = {}
        started = time.monotonic()
        for archive in (first, second):
            with zipfile.ZipFile(archive) as opened:
                _validated, claimed = mi.validate_wheel_layout(opened, site_packages, claimed)
        elapsed = time.monotonic() - started
        assert len(claimed) >= 3_000  # every file, plus its implied directories
        assert elapsed < 5.0, f"validating 3,000 members took {elapsed:.1f}s"


def test_primitives_do_not_import_heavy_dependencies() -> None:
    tree = ast.parse(Path(mi.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "torch" not in imported
    assert "huggingface_hub" not in imported
    assert "PySide6" not in imported
