"""Tests for the maintainer-only CUDA wheel lock generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "lock_cuda_runtime.py"


def load_lock_script():
    spec = importlib.util.spec_from_file_location("lock_cuda_runtime", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def report_entry(filename: str, url: str) -> dict:
    return {
        "metadata": {"name": filename.split("-", 1)[0]},
        "download_info": {"url": url},
    }


def test_lock_script_rejects_non_allowlisted_url() -> None:
    lock = load_lock_script()
    report = {"install": [report_entry("torch-2.8.0.whl", "https://example.invalid/torch.whl")]}
    fetches = []

    with pytest.raises(lock.LockError, match="allowlisted"):
        lock.lock_report(report, fetch_bytes=lambda url: fetches.append(url))

    assert fetches == []


def test_fetch_url_rejects_redirect_to_non_allowlisted_host() -> None:
    lock = load_lock_script()
    requested = []

    class RedirectedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def geturl(self) -> str:
            return "https://example.invalid/torch.whl"

        def read(self) -> bytes:
            raise AssertionError("must not read a redirected unallowlisted URL")

    def fake_open(url: str) -> RedirectedResponse:
        requested.append(url)
        return RedirectedResponse()

    with pytest.raises(lock.LockError, match="allowlisted"):
        lock.fetch_url(
            "https://download.pytorch.org/whl/cu128/torch-2.8.0.whl",
            open_url=fake_open,
        )

    assert requested == ["https://download.pytorch.org/whl/cu128/torch-2.8.0.whl"]


def test_lock_report_emits_sorted_immutable_wheel_records() -> None:
    lock = load_lock_script()
    zeta_url = "https://files.pythonhosted.org/packages/aa/zeta-1.0.0-py3-none-any.whl"
    alpha_url = (
        "https://download.pytorch.org/whl/cu128/alpha-2.8.0%2Bcu128-cp313-cp313-win_amd64.whl"
    )
    wheel_bytes = {
        zeta_url: b"zeta",
        alpha_url: b"alpha",
    }
    report = {
        "install": [
            report_entry("zeta-1.0.0-py3-none-any.whl", next(iter(wheel_bytes))),
            report_entry(
                "alpha-2.8.0+cu128-cp313-cp313-win_amd64.whl",
                list(wheel_bytes)[1],
            ),
        ]
    }

    records = lock.lock_report(report, fetch_bytes=wheel_bytes.__getitem__)
    rendered = lock.render_records(records)

    assert [record.filename for record in records] == [
        "alpha-2.8.0+cu128-cp313-cp313-win_amd64.whl",
        "zeta-1.0.0-py3-none-any.whl",
    ]
    assert records[0].url == alpha_url
    assert records[0].size_bytes == len(b"alpha")
    assert records[0].sha256 == hashlib.sha256(b"alpha").hexdigest()
    assert f'url="{zeta_url}"' in rendered
    assert "size_bytes=5" in rendered
    assert hashlib.sha256(b"zeta").hexdigest() in rendered


def test_resolve_report_accepts_fixture_command_seam(tmp_path: Path) -> None:
    lock = load_lock_script()
    calls = []
    expected = {
        "install": [report_entry("torch-2.8.0.whl", "https://download.pytorch.org/torch.whl")]
    }

    def fake_run(command: list[str], *, check: bool) -> None:
        calls.append((command, check))
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps(expected),
            encoding="utf-8",
        )

    actual = lock.resolve_report("windows-x64", tmp_path / "report.json", run_command=fake_run)
    assert actual == expected
    command, check = calls[0]
    assert check is True
    assert command[command.index("--index-url") + 1] == lock.PYTORCH_INDEX
    assert command[command.index("--extra-index-url") + 1] == lock.PYPI_INDEX
