#!/usr/bin/env python
"""Generate reviewable, checksum-pinned CUDA runtime wheel records.

This is a maintainer tool.  It uses pip only to produce a JSON resolution
report; the application runtime neither invokes this script nor pip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

PYTORCH_INDEX = "https://download.pytorch.org/whl/cu128"
PYPI_INDEX = "https://pypi.org/simple"
ALLOWLISTED_HOSTS = frozenset({"download.pytorch.org", "files.pythonhosted.org"})
REQUIREMENTS = ("torch==2.8.0", "torchaudio==2.8.0", "transformers==4.57.6")
PLATFORMS = {
    "windows-x64": "win_amd64",
    "linux-x64": "manylinux_2_28_x86_64",
}


class LockError(ValueError):
    """Raised when a resolver result cannot be made into an immutable lock."""


@dataclass(frozen=True)
class WheelRecord:
    filename: str
    url: str
    size_bytes: int
    sha256: str


def validate_url(url: str) -> str:
    """Return the wheel filename when *url* is a direct official wheel URL."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWLISTED_HOSTS:
        raise LockError(f"wheel URL is not allowlisted: {url}")
    if parsed.query or parsed.fragment:
        raise LockError(f"wheel URL must be immutable and query-free: {url}")
    filename = Path(unquote(parsed.path)).name
    if not filename.endswith(".whl"):
        raise LockError(f"wheel URL does not name a wheel: {url}")
    return filename


def lock_report(
    report: dict,
    fetch_bytes: Callable[[str], bytes],
) -> tuple[WheelRecord, ...]:
    """Download each report URL through an injectable HTTP seam and lock it."""
    records = []
    for item in report.get("install", []):
        url = item.get("download_info", {}).get("url")
        if not isinstance(url, str):
            raise LockError("pip report entry is missing download_info.url")
        filename = validate_url(url)
        payload = fetch_bytes(url)
        if not isinstance(payload, bytes):
            raise LockError(f"wheel download did not return bytes: {url}")
        records.append(
            WheelRecord(
                filename=filename,
                url=url,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    if not records:
        raise LockError("pip report contains no install records")
    return tuple(sorted(records, key=lambda record: record.filename))


def fetch_url(url: str, open_url: Callable[[str], object] = urlopen) -> bytes:
    """Fetch a direct wheel URL. Kept separate to make HTTP fixture-driven."""
    validate_url(url)
    with open_url(url) as response:  # noqa: S310 - URL is validated before fetch.
        final_url = response.geturl()
        validate_url(final_url)
        if final_url != url:
            raise LockError(f"wheel URL redirected: {url} -> {final_url}")
        return response.read()


def resolve_report(
    platform_key: str,
    report_path: Path,
    run_command: Callable[..., object] = subprocess.run,
) -> dict:
    """Ask pip for its JSON resolution report for one supported target."""
    try:
        pip_platform = PLATFORMS[platform_key]
    except KeyError as error:
        supported = ", ".join(sorted(PLATFORMS))
        raise LockError(f"unsupported platform {platform_key!r}; choose {supported}") from error

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--only-binary=:all:",
        "--report",
        str(report_path),
        "--platform",
        pip_platform,
        "--python-version",
        "3.13",
        "--implementation",
        "cp",
        "--abi",
        "cp313",
        "--index-url",
        PYTORCH_INDEX,
        "--extra-index-url",
        PYPI_INDEX,
        *REQUIREMENTS,
    ]
    run_command(command, check=True)
    return json.loads(report_path.read_text(encoding="utf-8"))


def render_records(records: tuple[WheelRecord, ...]) -> str:
    """Render records ready to paste into ``CudaRuntimeManifest.wheels``."""
    lines = ["wheels=("]
    for record in records:
        lines.extend(
            [
                "    RuntimeWheel(",
                f'        filename="{record.filename}",',
                f'        url="{record.url}",',
                f"        size_bytes={record.size_bytes},",
                f'        sha256="{record.sha256}",',
                "    ),",
            ]
        )
    lines.append(")")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="vienetts-cuda-lock-") as directory:
            report = resolve_report(args.platform, Path(directory) / "pip-report.json")
        records = lock_report(report, fetch_url)
        args.output.write_text(render_records(records), encoding="utf-8")
    except (LockError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"CUDA lock generation failed: {error}", file=sys.stderr)
        return 1
    print(f"locked {len(records)} wheels for {args.platform} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
