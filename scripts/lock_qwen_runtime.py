#!/usr/bin/env python
"""Render checksum-pinned Qwen runtime manifests for every matrix platform.

Maintainer tool.  It resolves each platform's declared closure with
``uv pip compile`` (metadata only: no wheel is downloaded for resolution),
then pins every resolved wheel by URL, size and SHA-256 into
``src/vienetts_app/core/qwen_runtime_manifests.json``.  The application runtime
neither runs this script nor needs uv.

Usage:
    uv run python scripts/lock_qwen_runtime.py            # re-lock every platform
    uv run python scripts/lock_qwen_runtime.py --check    # fail on drift (CI)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "packaging" / "qwen-runtime-requirements.json"
DEFAULT_OUTPUT = REPO_ROOT / "src" / "vienetts_app" / "core" / "qwen_runtime_manifests.json"
FORMAT_VERSION = "qwen-runtime-v1"
ALLOWLISTED_HOSTS = frozenset({"download.pytorch.org", "files.pythonhosted.org"})

# Platform key -> uv's --python-platform target.
PLATFORM_TARGETS = {
    "windows-x64-cpu": "x86_64-pc-windows-msvc",
    "windows-x64-cuda": "x86_64-pc-windows-msvc",
    "linux-x64-cpu": "x86_64-unknown-linux-gnu",
    "linux-x64-cuda": "x86_64-unknown-linux-gnu",
    "macos-arm64-cpu": "aarch64-apple-darwin",
    "macos-arm64-mps": "aarch64-apple-darwin",
}

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>\S+)\s*\\?\s*$")
_HASH = re.compile(r"--hash=sha256:(?P<sha256>[0-9a-f]{64})")
_INDEX_LINK = re.compile(r'href="(?P<href>[^"#]+)#sha256=(?P<sha256>[0-9a-f]{64})"')

# Specificity of a wheel's platform tag; higher wins when several wheels for the
# same distribution, version and Python tag are available.
_PLATFORM_RANK = {
    "manylinux_2_28": 60,
    "manylinux_2_27": 55,
    "manylinux_2_24": 50,
    "manylinux_2_17": 45,
    "manylinux2014": 45,
    "manylinux2010": 40,
    "manylinux1": 35,
    "macosx_11_0": 60,
    "macosx_10_9": 50,
    "win_amd64": 60,
    "linux_x86_64": 20,
    "any": 0,
}


class LockError(ValueError):
    """Raised when a resolved artifact cannot be pinned exactly."""


@dataclass(frozen=True)
class LockedPin:
    name: str
    version: str
    hashes: frozenset[str]


@dataclass(frozen=True)
class ArtifactFile:
    filename: str
    url: str
    sha256: str
    size_bytes: int | None = None


def parse_uv_lock(text: str) -> dict[str, LockedPin]:
    """Parse ``uv pip compile --generate-hashes`` output into pins."""
    pins: dict[str, LockedPin] = {}
    current: tuple[str, str] | None = None
    hashes: set[str] = set()

    def _flush() -> None:
        if current is not None:
            name, version = current
            if not hashes:
                raise LockError(f"resolved pin has no hashes: {name}=={version}")
            pins[name] = LockedPin(name, version, frozenset(hashes))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        if match is not None:
            _flush()
            current = (match.group("name"), match.group("version"))
            hashes = set()
            continue
        digest = _HASH.search(line)
        if digest is not None and current is not None:
            hashes.add(digest.group("sha256"))
    _flush()
    if not pins:
        raise LockError("resolver produced no pins")
    return pins


def validate_artifact_url(url: str, filename: str) -> None:
    """Reject anything that is not a direct, immutable wheel URL."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWLISTED_HOSTS:
        raise LockError(f"artifact URL is not allowlisted: {url}")
    if parsed.query or parsed.fragment:
        raise LockError(f"artifact URL must be immutable and query-free: {url}")
    if Path(unquote(parsed.path)).name != filename:
        raise LockError(f"artifact URL does not name {filename}: {url}")


def _platform_rank(platform_field: str) -> int:
    """Preference among platform tags: higher wins (newer, more specific)."""
    parts = platform_field.split(".")
    ranks = []
    for part in parts:
        for prefix, rank in _PLATFORM_RANK.items():
            if part == prefix or part.startswith(f"{prefix}_"):
                ranks.append(rank)
    return max(ranks, default=10)


def _macos_rank(platform_field: str) -> int:
    """Preference among macOS wheels: the *lowest* deployment target wins.

    A wheel tagged ``macosx_12_0_arm64`` runs on every macOS 12+, while
    ``macosx_14_0_arm64`` raises the app's floor to macOS 14. With no runtime
    OS to probe, the widest compatible wheel is the right pin.
    """
    versions = []
    for part in platform_field.split("."):
        fields = part.split("_")
        if fields[0] == "macosx" and len(fields) >= 3:
            try:
                versions.append(int(fields[1]) * 100 + int(fields[2]))
            except ValueError:
                continue
    return -min(versions, default=0)


def _python_rank(python_field: str, abi_field: str, python_tag: str) -> int | None:
    """Rank how well a wheel's Python/ABI tags fit this interpreter (None: no fit)."""
    if abi_field == f"{python_tag}t" or python_field == f"{python_tag}t":
        # Free-threaded build: a GIL-enabled interpreter cannot load it.
        return None
    if python_field == python_tag:
        return 3
    if python_field in {"py3", "py2.py3", "py2"}:
        return 0
    if python_field.startswith("cp3") and abi_field == "abi3":
        try:
            required = int(python_field[2:])
        except ValueError:
            return None
        return 2 if required <= int(python_tag[2:]) else None
    return None


def wheel_score(filename: str, python_tag: str, platform_tag: str) -> tuple[int, int, int] | None:
    """Rank a wheel filename for ``python_tag``/``platform_tag`` (higher wins)."""
    if not filename.endswith(".whl"):
        return None
    fields = filename[: -len(".whl")].split("-")
    if len(fields) < 5:
        return None
    python_field, abi_field, platform_field = fields[-3], fields[-2], fields[-1]
    python_rank = _python_rank(python_field, abi_field, python_tag)
    if python_rank is None:
        return None
    platform_parts = platform_field.split(".")
    if platform_tag in platform_parts:
        return (python_rank, 2, _platform_rank(platform_field))
    if platform_parts == ["any"]:
        return (python_rank, 1, 0)
    if platform_tag.startswith("linux") and any(
        part.startswith("manylinux") and part.endswith("x86_64") for part in platform_parts
    ):
        return (python_rank, 2, _platform_rank(platform_field))
    if platform_tag.startswith("macosx") and any(
        part.startswith("macosx") and part.endswith("arm64") for part in platform_parts
    ):
        return (python_rank, 2, _macos_rank(platform_field))
    return None


def select_artifact(
    files: Sequence[ArtifactFile],
    hashes: frozenset[str],
    python_tag: str,
    platform_tag: str,
) -> ArtifactFile | None:
    """Pick the single wheel the resolver pinned, or None when it has no wheel.

    Candidates are restricted to the digests the resolver reported for this
    platform, so a compromised or drifted index cannot swap the artifact.
    """
    scored: list[tuple[tuple[int, int, int, str], ArtifactFile]] = []
    for candidate in files:
        if candidate.sha256 not in hashes:
            continue
        score = wheel_score(candidate.filename, python_tag, platform_tag)
        if score is None:
            continue
        validate_artifact_url(candidate.url, candidate.filename)
        scored.append(((*score, candidate.filename), candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    best_score = scored[-1][0][:3]
    winners = [item for item in scored if item[0][:3] == best_score]
    if len(winners) > 1:
        filenames = sorted(item[1].filename for item in winners)
        raise LockError(f"ambiguous wheel choice: {filenames}")
    return winners[0][1]


def pypi_files(
    name: str,
    version: str,
    fetch_json: Callable[[str], object],
) -> tuple[ArtifactFile, ...]:
    payload = fetch_json(f"https://pypi.org/pypi/{name}/{version}/json")
    if not isinstance(payload, Mapping):
        raise LockError(f"PyPI metadata is malformed for {name}=={version}")
    urls = payload.get("urls")
    if not isinstance(urls, list):
        raise LockError(f"PyPI metadata has no files for {name}=={version}")
    files = []
    for entry in urls:
        if not isinstance(entry, Mapping):
            continue
        digests = entry.get("digests")
        sha256 = digests.get("sha256") if isinstance(digests, Mapping) else None
        size = entry.get("size")
        if not isinstance(sha256, str) or not isinstance(size, int):
            continue
        files.append(
            ArtifactFile(
                filename=str(entry.get("filename", "")),
                url=str(entry.get("url", "")),
                sha256=sha256,
                size_bytes=size,
            )
        )
    return tuple(files)


def index_files(
    index_url: str,
    name: str,
    fetch_text: Callable[[str], str],
) -> tuple[ArtifactFile, ...]:
    """Wheel files advertised by a PEP 503 index page (the PyTorch index).

    PyTorch's index links point at a CDN host (``download-r2.pytorch.org``)
    that is not the documented artifact host. The href is therefore reduced to
    its path and re-hosted on the index host, and the digest from the index
    fragment is what the manifest pins: a wrong file cannot pass verification.
    """
    parsed_index = urlsplit(index_url)
    host = parsed_index.hostname or ""
    if host not in ALLOWLISTED_HOSTS:
        raise LockError(f"index host is not allowlisted: {index_url}")
    try:
        html = fetch_text(f"{index_url.rstrip('/')}/{name}/")
    except LockError:
        # The index simply does not host this distribution (403/404).
        return ()
    files = []
    for match in _INDEX_LINK.finditer(html):
        path = urlsplit(match.group("href")).path
        if not path.startswith("/whl/"):
            # Navigation or project links are not artifacts; the digest check
            # below decides which candidate is the pinned wheel.
            continue
        filename = Path(unquote(path)).name
        files.append(
            ArtifactFile(
                filename=filename,
                url=f"https://{host}{path}",
                sha256=match.group("sha256"),
            )
        )
    return tuple(files)


def _with_size(artifact: ArtifactFile, fetch_size: Callable[[str], int]) -> ArtifactFile:
    if artifact.size_bytes is not None:
        return artifact
    return ArtifactFile(
        filename=artifact.filename,
        url=artifact.url,
        sha256=artifact.sha256,
        size_bytes=fetch_size(artifact.url),
    )


def resolve_artifact(
    pin: LockedPin,
    indexes: Mapping[str, str],
    *,
    python_tag: str,
    platform_tag: str,
    fetch_json: Callable[[str], object],
    fetch_text: Callable[[str], str],
) -> ArtifactFile | None:
    """Select the pinned wheel for one dependency, or None when it has none.

    PyPI is consulted first (cheap, and the only source of pure-Python wheels);
    the PyTorch indexes are tried only when PyPI has no matching digest. The
    resolver's digest set decides every choice, so a version published on both
    indexes cannot be confused.
    """
    if "+" not in pin.version:
        try:
            files = pypi_files(pin.name, pin.version, fetch_json)
        except LockError:
            files = ()
        selected = select_artifact(files, pin.hashes, python_tag, platform_tag)
        if selected is not None:
            return selected
    for key in sorted(indexes):
        index_url = indexes[key]
        if "pytorch.org" not in index_url:
            continue
        selected = select_artifact(
            index_files(index_url, pin.name, fetch_text),
            pin.hashes,
            python_tag,
            platform_tag,
        )
        if selected is not None:
            return selected
    return None


def build_platform_manifest(
    entry: Mapping[str, object],
    requirements: Mapping[str, object],
    *,
    python_version: str,
    python_tag: str,
    resolver: Callable[[Mapping[str, object], str], str],
    fetch_json: Callable[[str], object],
    fetch_text: Callable[[str], str],
    fetch_size: Callable[[str], int],
) -> dict[str, object]:
    """Resolve one platform's closure and pin every wheel it installs."""
    platform_key = str(entry["key"])
    platform_tag = str(entry["platformTag"])
    indexes = dict(requirements["indexes"])  # type: ignore[arg-type]
    primary_index = str(indexes[str(entry["index"])])
    lock_text = resolver(entry, python_version)
    pins = parse_uv_lock(lock_text)

    wheels: list[dict[str, object]] = []
    sdist_only: list[dict[str, str]] = []
    for name in sorted(pins):
        pin = pins[name]
        selected = resolve_artifact(
            pin,
            indexes,
            python_tag=python_tag,
            platform_tag=platform_tag,
            fetch_json=fetch_json,
            fetch_text=fetch_text,
        )
        if selected is None:
            sdist_only.append(
                {
                    "name": pin.name,
                    "version": pin.version,
                    "reason": (
                        f"no wheel for {platform_tag}/{python_tag} on the pinned indexes; "
                        "the isolated runtime is wheel-only, so this declared dependency "
                        "is not installed"
                    ),
                }
            )
            continue
        sized = _with_size(selected, fetch_size)
        assert sized.size_bytes is not None
        wheels.append(
            {
                "filename": sized.filename,
                "url": sized.url,
                "sizeBytes": sized.size_bytes,
                "sha256": sized.sha256,
            }
        )

    if not any(wheel["filename"].startswith("torch-") for wheel in wheels):
        raise LockError(f"closure for {platform_key} did not resolve torch")

    return {
        "platformTag": platform_tag,
        "device": entry["device"],
        "pythonTag": python_tag,
        "torchLocalVersion": pins["torch"].version,
        "pins": {name: pins[name].version for name in sorted(pins)},
        "wheels": wheels,
        "sdistOnly": sdist_only,
        "metadata": {
            "index": primary_index,
            "pythonVersion": python_version,
            "requirements": list(entry["requirements"]),  # type: ignore[arg-type]
        },
    }


def render_manifests(
    requirements: Mapping[str, object],
    *,
    python_version: str,
    platforms: Sequence[Mapping[str, object]],
    resolver: Callable[[Mapping[str, object], str], str],
    fetch_json: Callable[[str], object],
    fetch_text: Callable[[str], str],
    fetch_size: Callable[[str], int],
) -> dict[str, object]:
    python_tag = f"cp{python_version.replace('.', '')}"
    rendered: dict[str, object] = {
        "formatVersion": FORMAT_VERSION,
        "pythonVersion": python_version,
        "pythonTag": python_tag,
        "platforms": {
            str(entry["key"]): build_platform_manifest(
                entry,
                requirements,
                python_version=python_version,
                python_tag=python_tag,
                resolver=resolver,
                fetch_json=fetch_json,
                fetch_text=fetch_text,
                fetch_size=fetch_size,
            )
            for entry in platforms
        },
    }
    return rendered


def uv_resolver(
    uv_path: str,
    requirements_path: Path,
) -> Callable[[Mapping[str, object], str], str]:
    """Build the ``uv pip compile`` resolver used for a real re-lock."""

    def resolve(entry: Mapping[str, object], python_version: str) -> str:
        platform_key = str(entry["key"])
        target = PLATFORM_TARGETS.get(platform_key)
        if target is None:
            raise LockError(f"no uv target for platform: {platform_key}")
        requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        indexes = dict(requirements["indexes"])
        primary = str(indexes[str(entry["index"])])
        extras = [url for key, url in indexes.items() if url != primary]
        with tempfile.TemporaryDirectory() as tmp:
            pin_file = Path(tmp) / "requirements.txt"
            pin_file.write_text(
                "\n".join(str(item) for item in entry["requirements"]) + "\n",  # type: ignore[index]
                encoding="utf-8",
            )
            command = [
                uv_path,
                "pip",
                "compile",
                str(pin_file),
                "--generate-hashes",
                "--no-header",
                "--quiet",
                "--python-version",
                python_version,
                "--python-platform",
                target,
                "--index-url",
                primary,
                "--index-strategy",
                "unsafe-best-match",
            ]
            for url in extras:
                command += ["--extra-index-url", url]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise LockError(
                    f"uv could not resolve {platform_key}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            return result.stdout

    return resolve


def _fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "vienetts-app-lock/1.0"})
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - allowlisted hosts
            return response.read()
    except (HTTPError, URLError) as exc:
        raise LockError(f"could not fetch {url}: {exc}") from exc


def _fetch_json(url: str) -> object:
    return json.loads(_fetch_bytes(url))


def _fetch_text(url: str) -> str:
    return _fetch_bytes(url).decode("utf-8", errors="replace")


def _fetch_size(url: str) -> int:
    request = Request(url, method="HEAD", headers={"User-Agent": "vienetts-app-lock/1.0"})
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - allowlisted hosts
            length = response.headers.get("Content-Length")
    except (HTTPError, URLError) as exc:
        raise LockError(f"could not size {url}: {exc}") from exc
    if length is None:
        raise LockError(f"artifact has no Content-Length: {url}")
    return int(length)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--python-version", default="3.13")
    parser.add_argument("--platform", action="append", default=None)
    parser.add_argument("--check", action="store_true", help="fail when the lock is stale")
    parser.add_argument("--uv", default=shutil.which("uv") or "uv")
    args = parser.parse_args(argv)

    requirements = json.loads(args.requirements.read_text(encoding="utf-8"))
    platforms = [
        entry
        for entry in requirements["platforms"]
        if args.platform is None or entry["key"] in args.platform
    ]
    if not platforms:
        print("no platforms selected", file=sys.stderr)
        return 2
    if shutil.which(args.uv) is None:
        print(f"uv is required to re-lock the Qwen runtime: {args.uv!r}", file=sys.stderr)
        return 2

    try:
        rendered = render_manifests(
            requirements,
            python_version=args.python_version,
            platforms=platforms,
            resolver=uv_resolver(args.uv, args.requirements),
            fetch_json=_fetch_json,
            fetch_text=_fetch_text,
            fetch_size=_fetch_size,
        )
    except (LockError, HTTPError, URLError, OSError, KeyError) as exc:
        print(f"lock failed: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(rendered, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != payload:
            print(f"{args.output} is stale; re-run without --check", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date")
        return 0

    args.output.write_text(payload, encoding="utf-8")
    total = sum(
        wheel["sizeBytes"]
        for platform in rendered["platforms"].values()  # type: ignore[union-attr]
        for wheel in platform["wheels"]  # type: ignore[index]
    )
    print(
        f"wrote {args.output} for {len(rendered['platforms'])} platforms "  # type: ignore[arg-type]
        f"({total} bytes of pinned wheels)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
