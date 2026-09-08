"""Qwen model install/readiness state (Phase 3 Task 3)."""

import pytest
from huggingface_hub.utils import LocalEntryNotFoundError

from vienetts_app.core.qwen_models import (
    QwenModelStatus,
    ensure_qwen_model,
    qwen_model_status,
)


def not_cached(*args, **kwargs):
    raise LocalEntryNotFoundError("not cached")


def cached(path="/cache/qwen"):
    def probe(*args, **kwargs):
        return path

    return probe


class TestStatus:
    def test_ready_when_cached(self) -> None:
        status = qwen_model_status("qwen_customvoice", snapshot_fn=cached())
        assert isinstance(status, QwenModelStatus)
        assert status.state == "ready"
        assert status.local_dir == "/cache/qwen"
        assert status.error == ""

    def test_missing_when_not_cached(self) -> None:
        status = qwen_model_status("qwen_base", snapshot_fn=not_cached)
        assert status.state == "missing"
        assert status.local_dir is None

    def test_error_state_on_probe_failure(self) -> None:
        def boom(*args, **kwargs):
            raise OSError("disk gone")

        status = qwen_model_status("qwen_base", snapshot_fn=boom)
        assert status.state == "error"
        assert "disk gone" in status.error

    def test_unknown_engine_rejected(self) -> None:
        from vienetts_app.core.backends import BackendCapabilityError

        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            qwen_model_status("bogus", snapshot_fn=cached())  # type: ignore[arg-type]


class TestEnsure:
    def test_ready_short_circuits_download(self, tmp_path) -> None:
        calls: list = []
        status = ensure_qwen_model(
            "qwen_customvoice",
            snapshot_fn=cached(str(tmp_path)),
            size_probe=lambda repo: None,
        )
        assert status.state == "ready"
        assert calls == []

    def test_download_reports_progress(self) -> None:
        seen: list[tuple] = []

        class FakeTqdm:
            def __init__(self, total=None, progress_cb=None, **kw):
                self.total = total or 0
                self.n = 0
                self._cb = progress_cb

            def update(self, n):
                self.n += n
                if self._cb is not None:
                    self._cb(self.n, self.total)

            def close(self):
                pass

        def download(repo, tqdm_class=None, local_files_only=False, **kw):
            if local_files_only:
                raise LocalEntryNotFoundError("not cached")
            bar = tqdm_class(total=1000)
            for _ in range(4):
                bar.update(250)
            bar.close()
            return "/cache/new"

        status = ensure_qwen_model(
            "qwen_base",
            snapshot_fn=download,
            tqdm_class=FakeTqdm,
            progress_cb=lambda done, total: seen.append(("cb", done, total)),
            size_probe=lambda repo: None,
        )
        assert status.state == "ready"
        assert status.local_dir == "/cache/new"
        assert ("cb", 1000, 1000) in seen

    def test_preflight_blocks_without_space(self) -> None:
        calls: list = []

        def download(*args, local_files_only=False, **kwargs):
            if local_files_only:
                raise LocalEntryNotFoundError("not cached")
            calls.append(1)
            return "/cache/new"

        status = ensure_qwen_model(
            "qwen_base",
            snapshot_fn=download,
            size_probe=lambda repo: 10_000_000_000,
            free_space_fn=lambda path: 1_000,
        )
        assert status.state == "error"
        assert "space" in status.error.lower()
        assert calls == []

    def test_cancel_before_start_skips_download(self) -> None:
        calls: list = []

        def download(*args, **kwargs):
            calls.append(1)
            return "/cache/new"

        status = ensure_qwen_model(
            "qwen_base",
            snapshot_fn=download,
            size_probe=lambda repo: None,
            cancelled=lambda: True,
        )
        assert status.state == "cancelled"

    def test_retry_after_failure(self) -> None:
        attempts: list = []

        def flaky(repo, tqdm_class=None, local_files_only=False, **kw):
            attempts.append(local_files_only)
            if local_files_only:
                raise LocalEntryNotFoundError("not cached")
            if len(attempts) == 2:
                raise OSError("network blip")
            return "/cache/new"

        first = ensure_qwen_model("qwen_base", snapshot_fn=flaky, size_probe=lambda r: None)
        assert first.state == "error"
        second = ensure_qwen_model("qwen_base", snapshot_fn=flaky, size_probe=lambda r: None)
        assert second.state == "ready"

    def test_download_failure_reports_error(self) -> None:
        def boom(repo, local_files_only=False, **kw):
            if local_files_only:
                raise LocalEntryNotFoundError("not cached")
            raise OSError("network down")

        status = ensure_qwen_model("qwen_base", snapshot_fn=boom, size_probe=lambda r: None)
        assert status.state == "error"
        assert "network down" in status.error
