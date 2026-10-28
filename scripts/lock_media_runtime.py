#!/usr/bin/env python3
"""Plan, stage, probe, pack and lock the project-published media runtime packs.

Maintainer/CI tool — the application never runs this. Every step reads the
pins in ``packaging/media-runtime-requirements.json`` and is driven by the
opt-in ``.github/workflows/media-runtime-build.yml``:

* ``plan``  — shrink the per-runtime GitHub matrices to the selected cells.
* ``fetch`` — download a cell's pinned upstream ffmpeg files and verify them
  against the pinned size + SHA-256 AND the upstream checksum file.
* ``stage`` — restage verified upstream files into the pack layout the app's
  installers expect (``bin/…`` + ``LICENSE``; Linux adds ``lib/``).
* ``smoke`` — run the cell's probes (``--help`` / ``-version`` /
  ``-encoders`` / ``-filters``) from a foreign cwd. This EXECUTES the staged
  binaries — CI only.
* ``pack``  — write ``SOURCE.txt`` and the pack archive (no top-level dir).
* ``lock``  — verify an archive (layout, probe transcripts, size cap) and
  record it in the lock by size + SHA-256 under a content-addressed path;
  ``--publish-root`` also copies it there (the gh-pages ``packs/`` tree).

``scripts/render_media_runtime_manifests.py`` turns the lock into the shipped
``src/vienetts_app/core/media_runtime_manifests.json``.

Usage:
    python scripts/lock_media_runtime.py plan --cells all
    python scripts/lock_media_runtime.py fetch --runtime ffmpeg --cell linux-x64 --dest DIR
    python scripts/lock_media_runtime.py stage --runtime ffmpeg --cell linux-x64 \\
        --downloads DIR --stage DIR
    python scripts/lock_media_runtime.py smoke --runtime R --cell C --stage DIR --out DIR
    python scripts/lock_media_runtime.py pack --runtime R --cell C --stage DIR --out DIR
    python scripts/lock_media_runtime.py lock --runtime R --cell C --pack FILE \\
        --probe-dir DIR [--lock FILE] [--publish-root DIR]
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import gzip
import hashlib
import importlib.util
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import IO
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("vienetts_app") is None:  # CI runs from a bare checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from vienetts_app.core import ai_models  # noqa: E402 - after the src bootstrap

DEFAULT_REQUIREMENTS = REPO_ROOT / "packaging" / "media-runtime-requirements.json"
DEFAULT_LOCK = REPO_ROOT / "packaging" / "media-runtime-lock.json"
LOCK_SCHEMA = 1
NOTICE_FILES = ("LICENSE", "SOURCE.txt")
#: ``fetch`` saves each asset's upstream checksum file as ``<asset><suffix>``.
UPSTREAM_CHECKSUM_SUFFIX = ".upstream.sha256"
PROBE_TIMEOUT_S = 120
#: Every archive member gets this mtime (1980-01-01, the zip format's floor).
_ARCHIVE_EPOCH = 315_532_800
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_COMMIT_HEX = re.compile(r"[0-9a-f]{40}")
#: Runtime-level keys every cell inherits unless it overrides them.
_RUNTIME_DEFAULTS = ("packPrefix", "version", "license", "source", "upstream", "binaries", "probes")
#: The cell keys that decide a pack's bytes: a change makes its lock stale.
_DIGEST_KEYS = (
    "packPrefix",
    "version",
    "archive",
    "upstream",
    "cmakeFlags",
    "stage",
    "binaries",
    "probes",
)


class LockError(RuntimeError):
    """Raised when a pack cannot be planned, fetched, staged, packed or locked."""


# ── Requirements ─────────────────────────────────────────────────────────────


def load_requirements(path: Path = DEFAULT_REQUIREMENTS) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def runtime_cells(requirements: Mapping) -> list[tuple[str, str]]:
    """Every ``(runtime, cell)`` the requirements declare, in file order."""
    return [
        (runtime, cell)
        for runtime, body in requirements["runtimes"].items()
        for cell in body["cells"]
    ]


def cell_spec(requirements: Mapping, runtime: str, cell: str) -> dict:
    """The cell's pins merged over its runtime defaults (plus ``runtime``/``cell``)."""
    body = requirements.get("runtimes", {}).get(runtime)
    if body is None or cell not in body.get("cells", {}):
        known = ", ".join(f"{r}/{c}" for r, c in runtime_cells(requirements))
        raise LockError(f"unknown cell {runtime}/{cell} — expected one of: {known}")
    spec = {key: copy.deepcopy(body[key]) for key in _RUNTIME_DEFAULTS if key in body}
    spec.update(copy.deepcopy(body["cells"][cell]))
    spec["runtime"], spec["cell"] = runtime, cell
    return spec


def _where(spec: Mapping) -> str:
    return f"{spec['runtime']}/{spec['cell']}"


def pin_problems(spec: Mapping) -> list[str]:
    """Unfilled upstream pins (placeholders) — empty when the cell is buildable."""
    where, upstream = _where(spec), spec.get("upstream") or {}
    problems: list[str] = []
    if "commit" in upstream and not _COMMIT_HEX.fullmatch(str(upstream["commit"])):
        problems.append(f"{where}: upstream commit {upstream['commit']!r} is a placeholder")
    for asset in upstream.get("assets", []):
        size, sha256 = asset.get("size"), str(asset.get("sha256", ""))
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            problems.append(f"{where}: {asset.get('name')} size pin is a placeholder")
        if not SHA256_HEX.fullmatch(sha256):
            source = asset.get("checksumUrl") or asset.get("url")
            problems.append(
                f"{where}: {asset.get('name')} sha256 pin is a placeholder — fill it from {source}"
            )
        if not str(asset.get("url", "")).startswith("https://"):
            problems.append(f"{where}: {asset.get('name')} has no pinned https URL")
    if "commit" not in upstream and not upstream.get("assets"):
        problems.append(f"{where}: no upstream pin (a source commit or pinned assets)")
    return problems


def _require_pins(spec: Mapping) -> None:
    problems = pin_problems(spec)
    if problems:
        raise LockError("; ".join(problems))


def spec_digest(spec: Mapping) -> str:
    """Digest of everything that decides a pack's bytes (stale-lock detection)."""
    pinned = {key: spec.get(key) for key in _DIGEST_KEYS}
    canonical = json.dumps(pinned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pack_filename(spec: Mapping) -> str:
    """The name ``pack`` writes: ``<prefix>-<version>-<cell>.<archive>``."""
    return f"{spec['packPrefix']}-{spec['version']}-{spec['cell']}.{spec['archive']}"


def published_path(requirements: Mapping, spec: Mapping, sha256: str) -> str:
    """Content-addressed path under the published ``packs/`` root.

    The SHA prefix in the name means a rebuild can never replace bytes an
    already-shipped manifest pins: every build gets its own immutable URL.
    """
    stem = pack_filename(spec).removesuffix(f".{spec['archive']}")
    return (
        f"{requirements['packDir']}/{spec['runtime']}/{spec['cell']}/"
        f"{stem}-{sha256[:12]}.{spec['archive']}"
    )


# ── plan ─────────────────────────────────────────────────────────────────────


def plan(requirements: Mapping, selection: str) -> dict[str, list[dict]]:
    """Matrix rows per runtime for ``all`` / ``<runtime>`` / ``<runtime>/<cell>`` items."""
    every = runtime_cells(requirements)
    wanted: list[tuple[str, str]] = []
    for item in (part.strip() for part in selection.split(",")):
        if not item:
            continue
        if item == "all":
            picked = every
        elif "/" in item:
            runtime, _, cell = item.partition("/")
            cell_spec(requirements, runtime, cell)
            picked = [(runtime, cell)]
        elif item in requirements["runtimes"]:
            picked = [pair for pair in every if pair[0] == item]
        else:
            raise LockError(f"unknown selection {item!r} — use all, a runtime or <runtime>/<cell>")
        wanted += [pair for pair in picked if pair not in wanted]
    if not wanted:
        raise LockError("no cells selected")
    planned: dict[str, list[dict]] = {runtime: [] for runtime in requirements["runtimes"]}
    for runtime, cell in sorted(wanted, key=every.index):
        spec = cell_spec(requirements, runtime, cell)
        _require_pins(spec)
        row = {
            "id": f"{runtime}-{cell}",
            "runtime": runtime,
            "cell": cell,
            "runner": spec["runner"],
            "os": spec["os"],
        }
        if "commit" in spec["upstream"]:
            row["repo"] = spec["upstream"]["repo"]
            row["commit"] = spec["upstream"]["commit"]
            row["cmake_flags"] = " ".join(spec.get("cmakeFlags", []))
        planned[runtime].append(row)
    return planned


def plan_outputs(planned: Mapping[str, list[dict]]) -> list[str]:
    """``$GITHUB_OUTPUT`` lines: ``<runtime>_cells`` (matrix JSON) + ``<runtime>_any``.

    An unselected runtime still gets a one-row placeholder matrix: its job is
    skipped through ``<runtime>_any == 'false'`` (a job-level ``if:`` may not
    read ``matrix``), and a never-empty matrix keeps the strategy evaluable.
    """
    lines: list[str] = []
    for runtime, rows in planned.items():
        matrix = {"include": rows or [{"id": "none"}]}
        lines.append(f"{runtime}_cells={json.dumps(matrix, separators=(',', ':'))}")
        lines.append(f"{runtime}_any={'true' if rows else 'false'}")
    return lines


# ── fetch / verify / stage (ffmpeg) ──────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    request = Request(url, headers={"User-Agent": "VieNeuTTSApp-media-runtime-build"})
    with urlopen(request, timeout=60) as response, target.open("wb") as fh:  # noqa: S310
        shutil.copyfileobj(response, fh, 1 << 20)


def fetch_upstream(spec: Mapping, dest: Path) -> None:
    """Download the cell's pinned upstream files (+ checksum files), then verify."""
    _require_pins(spec)
    dest.mkdir(parents=True, exist_ok=True)
    for asset in spec["upstream"].get("assets", []):
        _download(asset["url"], dest / asset["name"])
        if asset.get("checksumUrl"):
            _download(asset["checksumUrl"], dest / f"{asset['name']}{UPSTREAM_CHECKSUM_SUFFIX}")
    verify_upstream(spec, dest)


def _listed_checksum(listing: str, name: str) -> str | None:
    """The SHA-256 a ``sha256sum``-style listing records for ``name``."""
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            candidate = parts[0].lower()
            if SHA256_HEX.fullmatch(candidate):
                return candidate
    return None


def verify_upstream(spec: Mapping, downloads: Path) -> None:
    """Every upstream file matches its pin AND the upstream's published checksum."""
    _require_pins(spec)
    for asset in spec["upstream"].get("assets", []):
        name = asset["name"]
        path = downloads / name
        if not path.is_file():
            raise LockError(f"upstream file missing: {path}")
        size = path.stat().st_size
        if size != asset["size"]:
            raise LockError(f"{name} is {size} bytes, the pin says {asset['size']}")
        digest = sha256_file(path)
        if digest != asset["sha256"]:
            raise LockError(f"{name} does not match the pinned sha256")
        if not asset.get("checksumUrl"):
            continue
        listing_path = downloads / f"{name}{UPSTREAM_CHECKSUM_SUFFIX}"
        if not listing_path.is_file():
            raise LockError(f"upstream checksum file missing for {name}: {listing_path}")
        listed = _listed_checksum(listing_path.read_text(encoding="utf-8", errors="replace"), name)
        if listed is None:
            raise LockError(f"the upstream checksum file does not list {name}")
        if listed != digest:
            raise LockError(f"{name}: the upstream checksum file disagrees ({listed})")


def _iter_archive(path: Path) -> Iterator[tuple[str, str, object]]:
    """``(name, kind, payload)`` per non-directory member.

    ``kind`` is ``file`` (payload: a zero-arg opener), ``link`` (payload: the
    symlink target) or ``other`` (hard links, devices — never staged).
    """
    if path.name.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    yield info.filename, "file", (lambda info=info: zf.open(info))
        return
    with tarfile.open(path, "r:*") as tf:
        for member in tf.getmembers():
            if member.isdir():
                continue
            if member.issym():
                yield member.name, "link", member.linkname
            elif member.isfile():
                yield member.name, "file", (lambda member=member: tf.extractfile(member))
            else:
                yield member.name, "other", None


def _is_inside(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not name.startswith("/") and "\\" not in name and ".." not in parts


def _place(
    stage: Path,
    rel: str,
    placed: set[str],
    *,
    opener: Callable[[], IO[bytes] | None] | None = None,
    link: str | None = None,
) -> None:
    if not _is_inside(rel) or rel in placed:
        raise LockError(f"cannot stage {rel!r}: outside the pack or staged twice")
    target = stage / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if link is not None:
        if "/" in link or link in ("", ".", ".."):
            raise LockError(f"refusing the symlink {rel} -> {link}: not a sibling file")
        os.symlink(link, target)
    else:
        source = opener() if opener is not None else None
        if source is None:
            raise LockError(f"cannot read {rel!r} from the upstream archive")
        with source, target.open("wb") as fh:
            shutil.copyfileobj(source, fh, 1 << 20)
        target.chmod(0o755 if rel.startswith("bin/") else 0o644)
    placed.add(rel)


def stage_upstream(spec: Mapping, downloads: Path, stage: Path) -> None:
    """Restage verified upstream files per the cell's ``stage`` rules.

    A rule copies the archive members matching any of its ``members`` globs
    (``fnmatch``: ``*`` crosses ``/``) — or the asset itself when it lists no
    members — to ``dest``: a directory when it ends with ``/`` (base names
    kept), else exactly one file. Each rule must match something.
    """
    rules = spec.get("stage")
    if not rules:
        raise LockError(f"{_where(spec)} has no stage rules (it is built from source)")
    if stage.exists() and any(stage.iterdir()):
        raise LockError(f"stage dir {stage} is not empty")
    stage.mkdir(parents=True, exist_ok=True)
    placed: set[str] = set()
    for rule in rules:
        asset = downloads / rule["asset"]
        if not asset.is_file():
            raise LockError(f"upstream file missing: {asset}")
        dest, globs = rule["dest"], rule.get("members")
        if not globs:
            _place(stage, dest, placed, opener=lambda asset=asset: asset.open("rb"))
            continue
        matched = 0
        for name, kind, payload in _iter_archive(asset):
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in globs):
                continue
            if kind == "other":
                raise LockError(f"{asset.name}: {name} is neither a file nor a symlink")
            matched += 1
            rel = f"{dest}{PurePosixPath(name).name}" if dest.endswith("/") else dest
            if kind == "link":
                _place(stage, rel, placed, link=str(payload))
            else:
                _place(stage, rel, placed, opener=payload)  # type: ignore[arg-type]
        if matched == 0 or (not dest.endswith("/") and matched != 1):
            raise LockError(f"{asset.name}: {matched} members match {globs} for {dest!r}")
    missing = [rel for rel in (*spec["binaries"], "LICENSE") if rel not in placed]
    if missing:
        raise LockError(f"{_where(spec)}: the stage lacks {', '.join(missing)}")


# ── smoke ────────────────────────────────────────────────────────────────────


def _has_token(text: str, token: str) -> bool:
    """``token`` as a whole flag/word: ``--vad`` never matches ``--vad-model``."""
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def _listed_names(listing: str) -> set[str]:
    """The name column of ``ffmpeg -encoders`` / ``-filters`` (legend lines skipped)."""
    names: set[str] = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "=":
            names.add(parts[1])
    return names


def probe_problems(spec: Mapping, outputs: Mapping[str, str | None]) -> list[str]:
    """What the probe transcripts fail to prove (empty = the pack qualifies)."""
    problems: list[str] = []
    for probe in spec["probes"]:
        name, text = probe["name"], outputs.get(probe["name"])
        if text is None:
            problems.append(f"no {name} probe output")
            continue
        problems += [
            f"{name}: {token!r} not reported"
            for token in probe.get("contains", [])
            if not _has_token(text, token)
        ]
        problems += [
            f"{name}: {token!r} is reported — not redistributable"
            for token in probe.get("absent", [])
            if _has_token(text, token)
        ]
        listed = _listed_names(text) if probe.get("listed") else set()
        problems += [
            f"{name}: {item!r} is not listed"
            for item in probe.get("listed", [])
            if item not in listed
        ]
    return problems


def run_probes(spec: Mapping, stage: Path, out_dir: Path) -> dict[str, str]:
    """Run every probe from a foreign cwd, save the transcripts, verify them.

    EXECUTES the staged binaries — the workflow's smoke step, never the app.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="media-probe-") as cwd:
        for probe in spec["probes"]:
            rel = probe["argv"][0]
            exe = stage / rel
            if not exe.is_file() and (stage / f"{rel}.exe").is_file():
                exe = stage / f"{rel}.exe"
            if not exe.is_file():
                raise LockError(f"{_where(spec)}: {rel} is not in the stage")
            command = ai_models.cli_command(exe.resolve(), *probe["argv"][1:])
            try:
                proc = subprocess.run(
                    command,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=PROBE_TIMEOUT_S,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise LockError(f"{_where(spec)}: the {probe['name']} probe failed: {exc}") from exc
            text = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0:
                raise LockError(
                    f"{_where(spec)}: the {probe['name']} probe exited {proc.returncode}: "
                    f"{text[-500:]}"
                )
            (out_dir / f"{probe['name']}.txt").write_text(text, encoding="utf-8")
            outputs[probe["name"]] = text
    problems = probe_problems(spec, outputs)
    if problems:
        raise LockError(f"{_where(spec)}: " + "; ".join(problems))
    return outputs


# ── pack ─────────────────────────────────────────────────────────────────────


def source_notice(spec: Mapping) -> str:
    """``SOURCE.txt``: where the binaries and their corresponding source come from."""
    upstream = spec["upstream"]
    lines = [
        f"{spec['packPrefix']} {spec['version']} ({spec['cell']}) — redistributed by VieNeuTTSApp",
        f"License: {spec['license']} (full text in LICENSE)",
        f"Source: {spec['source']}",
    ]
    if "commit" in upstream:
        lines.append(
            f"Built from https://github.com/{upstream['repo']} "
            f"tag {upstream['tag']}, commit {upstream['commit']}"
        )
        lines.append("CMake flags: " + " ".join(spec.get("cmakeFlags", [])))
    else:
        lines.append(f"Upstream build: {upstream['provider']} {upstream['release']}")
        lines.append(f"FFmpeg source: {upstream['ffmpegSource']}")
        lines.append(f"Build scripts: {upstream['buildScripts']}")
        lines.append("Upstream files (sha256):")
        lines += [f"  {asset['url']}  {asset['sha256']}" for asset in upstream["assets"]]
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        lines.append(
            f"Packed by: {server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{run_id}"
        )
    return "\n".join(lines) + "\n"


def layout_problems(spec: Mapping, members: Mapping[str, str | None]) -> list[str]:
    """Pack layout violations; ``members`` maps member name → symlink target or None."""
    problems: list[str] = []
    for name, link in members.items():
        if not _is_inside(name):
            problems.append(f"{name!r} points outside the install dir")
        elif link is not None and (link.startswith("/") or not _is_inside(link)):
            problems.append(f"symlink {name} -> {link} points outside the install dir")
    for required in (*spec["binaries"], *NOTICE_FILES):
        if required not in members or members[required] is not None:
            problems.append(f"missing {required}")
    return problems


def _stage_entries(stage: Path) -> list[tuple[str, Path, str | None]]:
    entries: list[tuple[str, Path, str | None]] = []
    for path in sorted(stage.rglob("*")):
        rel = path.relative_to(stage).as_posix()
        if path.is_symlink():
            entries.append((rel, path, os.readlink(path)))
        elif path.is_file():
            entries.append((rel, path, None))
    return entries


def _member_mode(rel: str) -> int:
    return 0o755 if rel.startswith("bin/") else 0o644


def _write_tar(fileobj: IO[bytes], entries: list[tuple[str, Path, str | None]]) -> None:
    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for rel, path, link in entries:
            info = tarfile.TarInfo(rel)
            info.mtime, info.uid, info.gid = _ARCHIVE_EPOCH, 0, 0
            if link is not None:
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, link, 0o777
                tf.addfile(info)
                continue
            info.size, info.mode = path.stat().st_size, _member_mode(rel)
            with path.open("rb") as fh:
                tf.addfile(info, fh)


def _write_archive(pack: Path, archive: str, entries: list[tuple[str, Path, str | None]]) -> None:
    """Deterministic archive: sorted members, fixed mtime/owner, no top-level dir."""
    part = pack.with_name(f"{pack.name}.part")
    if archive == "zip":
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, path, link in entries:
                if link is not None:
                    raise LockError(f"a zip pack cannot carry the symlink {rel}")
                info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (0o100000 | _member_mode(rel)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes(), compresslevel=9)
    elif archive == "tar.gz":
        with (
            part.open("wb") as raw,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=_ARCHIVE_EPOCH
            ) as gz,
        ):
            _write_tar(gz, entries)
    elif archive == "tar.xz":
        with lzma.open(part, "wb") as xz:  # xz -6: preset 9 needs ~700 MB of RAM
            _write_tar(xz, entries)
    else:
        raise LockError(f"unsupported pack archive {archive!r}")
    os.replace(part, pack)


def _check_size(requirements: Mapping, pack: Path) -> None:
    size, limit = pack.stat().st_size, int(requirements["maxPackBytes"])
    if size > limit:
        raise LockError(
            f"{pack.name} is {size} bytes, over the {limit}-byte gh-pages file limit "
            "(GitHub rejects pushed files over 100 MiB)"
        )


def build_pack(requirements: Mapping, spec: Mapping, stage: Path, out_dir: Path) -> Path:
    """Write ``SOURCE.txt`` into ``stage`` and archive it as ``pack_filename(spec)``."""
    if not stage.is_dir():
        raise LockError(f"stage dir {stage} does not exist")
    (stage / "SOURCE.txt").write_text(source_notice(spec), encoding="utf-8")
    entries = _stage_entries(stage)
    problems = layout_problems(spec, {rel: link for rel, _, link in entries})
    if problems:
        raise LockError(f"{_where(spec)}: " + "; ".join(problems))
    out_dir.mkdir(parents=True, exist_ok=True)
    pack = out_dir / pack_filename(spec)
    _write_archive(pack, spec["archive"], entries)
    _check_size(requirements, pack)
    return pack


# ── lock ─────────────────────────────────────────────────────────────────────


def _archive_members(pack: Path) -> dict[str, str | None]:
    members: dict[str, str | None] = {}
    if pack.name.endswith(".zip"):
        with zipfile.ZipFile(pack) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    members[info.filename] = None
        return members
    with tarfile.open(pack, "r:*") as tf:
        for member in tf.getmembers():
            if member.isdir():
                continue
            if not (member.isfile() or member.issym()):
                raise LockError(f"{pack.name}: {member.name} is neither a file nor a symlink")
            members[member.name] = member.linkname if member.issym() else None
    return members


def _publish(pack: Path, target: Path, sha256: str) -> None:
    """Copy ``pack`` to its content-addressed target; never replace other bytes."""
    if target.exists():
        if sha256_file(target) != sha256:
            raise LockError(f"refusing to replace the published {target} with different bytes")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.part")
    shutil.copyfile(pack, part)
    os.replace(part, target)


def lock_pack(
    requirements: Mapping,
    spec: Mapping,
    pack: Path,
    probe_dir: Path,
    lock_path: Path,
    *,
    publish_root: Path | None = None,
) -> dict:
    """Verify ``pack`` and record it in ``lock_path`` (merged, keyed ``runtime/cell``).

    Refuses a pack with unfilled pins, the wrong name, over the size cap, a
    member escaping the install dir, a missing binary/LICENSE/SOURCE.txt, or
    probe transcripts (``<probe>.txt`` in ``probe_dir``) that miss a token or
    report a forbidden one (``--enable-nonfree``).
    """
    _require_pins(spec)
    if pack.name != pack_filename(spec):
        raise LockError(f"{pack.name} is not the {_where(spec)} pack ({pack_filename(spec)})")
    _check_size(requirements, pack)
    problems = layout_problems(spec, _archive_members(pack))
    outputs: dict[str, str | None] = {}
    for probe in spec["probes"]:
        transcript = probe_dir / f"{probe['name']}.txt"
        outputs[probe["name"]] = (
            transcript.read_text(encoding="utf-8", errors="replace")
            if transcript.is_file()
            else None
        )
    problems += probe_problems(spec, outputs)
    if problems:
        raise LockError(f"{_where(spec)}: " + "; ".join(problems))
    digest = sha256_file(pack)
    entry = {
        "runtime": spec["runtime"],
        "cell": spec["cell"],
        "path": published_path(requirements, spec, digest),
        "archive": spec["archive"],
        "version": spec["version"],
        "size": pack.stat().st_size,
        "sha256": digest,
        "specDigest": spec_digest(spec),
        "upstream": spec["upstream"],
    }
    if publish_root is not None:
        _publish(pack, publish_root / entry["path"], digest)
    doc: dict = {"schemaVersion": LOCK_SCHEMA, "packs": {}}
    if lock_path.is_file():
        doc = json.loads(lock_path.read_text(encoding="utf-8"))
    packs = dict(doc.get("packs", {}))
    packs[_where(spec)] = entry
    doc["packs"] = dict(sorted(packs.items()))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return entry


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    commands = parser.add_subparsers(dest="command", required=True)

    plan_cmd = commands.add_parser("plan", help="print $GITHUB_OUTPUT matrix lines")
    plan_cmd.add_argument("--cells", required=True)
    for name in ("fetch", "stage", "smoke", "pack", "lock"):
        sub = commands.add_parser(name)
        sub.add_argument("--runtime", required=True)
        sub.add_argument("--cell", required=True)
        if name == "fetch":
            sub.add_argument("--dest", type=Path, required=True)
        if name == "stage":
            sub.add_argument("--downloads", type=Path, required=True)
        if name in ("stage", "smoke", "pack"):
            sub.add_argument("--stage", type=Path, required=True)
        if name in ("smoke", "pack"):
            sub.add_argument("--out", type=Path, required=True)
        if name == "lock":
            sub.add_argument("--pack", type=Path, required=True)
            sub.add_argument("--probe-dir", type=Path, required=True)
            sub.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
            sub.add_argument("--publish-root", type=Path, default=None)
    args = parser.parse_args(argv)
    requirements = load_requirements(args.requirements)

    if args.command == "plan":
        print("\n".join(plan_outputs(plan(requirements, args.cells))))
        return 0
    spec = cell_spec(requirements, args.runtime, args.cell)
    if args.command == "fetch":
        fetch_upstream(spec, args.dest)
        print(f"{_where(spec)}: upstream files match their pins and upstream checksums")
    elif args.command == "stage":
        verify_upstream(spec, args.downloads)
        stage_upstream(spec, args.downloads, args.stage)
        print(f"{_where(spec)}: staged into {args.stage}")
    elif args.command == "smoke":
        outputs = run_probes(spec, args.stage, args.out)
        print(f"{_where(spec)}: probes passed ({', '.join(outputs)})", file=sys.stderr)
    elif args.command == "pack":
        print(build_pack(requirements, spec, args.stage, args.out))
    else:
        entry = lock_pack(
            requirements,
            spec,
            args.pack,
            args.probe_dir,
            args.lock,
            publish_root=args.publish_root,
        )
        print(f"locked {entry['path']} ({entry['size']} bytes, sha256 {entry['sha256']})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
