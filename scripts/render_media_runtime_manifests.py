#!/usr/bin/env python3
"""Regenerate the shipped ``media_runtime_manifests.json`` from locked packs.

The shipped manifest is generated — never hand-edited. Every cell declared in
``packaging/media-runtime-requirements.json`` gets an entry (licence, source,
archive, version); only a cell with a pack in the lock
(``packaging/media-runtime-lock.json``, written by
``scripts/lock_media_runtime.py lock``) is ``published`` with its
content-addressed ``<downloads-base>/<path>`` URL, size and SHA-256. Every
other cell stays a truthful "not available yet".

Refused (exit 2): a locked cell the requirements do not declare, a cell whose
pins are still placeholders, a lock entry made from different requirements
(stale), and — with ``--packs-dir`` (the gh-pages ``packs/`` tree) — a locked
pack that is missing there or whose bytes differ from the lock. The output is
validated with the application's own manifest parser before it is written.

Usage:
    python scripts/render_media_runtime_manifests.py [--lock FILE] \\
        [--packs-dir DIR] [--downloads-base URL] [--out FILE] [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # ``python scripts/render_…`` puts scripts/ first
    sys.path.insert(0, str(REPO_ROOT))

from scripts import lock_media_runtime as lock  # noqa: E402 - after the sys.path bootstrap
from vienetts_app.core import ai_models  # noqa: E402

DEFAULT_OUT = ai_models.MEDIA_RUNTIME_MANIFEST_PATH
GENERATED_FROM = (
    "packaging/media-runtime-lock.json (locked packs) + "
    "packaging/media-runtime-requirements.json (cell contract), rendered by "
    "scripts/render_media_runtime_manifests.py — only cells that were built, "
    "probed, locked and published carry published=true with url/size/sha256; "
    "every other cell is a truthful 'not available yet'."
)


class RenderError(RuntimeError):
    """Raised when the lock cannot be rendered into a shippable manifest."""


def _check_entry(
    requirements: Mapping, spec: Mapping, entry: Mapping, packs_dir: Path | None
) -> None:
    where = f"{spec['runtime']}/{spec['cell']}"
    problems = lock.pin_problems(spec)
    if problems:
        raise RenderError("; ".join(problems))
    sha256, size = str(entry.get("sha256", "")), entry.get("size")
    if (
        entry.get("specDigest") != lock.spec_digest(spec)
        or not lock.SHA256_HEX.fullmatch(sha256)
        or entry.get("path") != lock.published_path(requirements, spec, sha256)
    ):
        raise RenderError(
            f"{where}: the lock entry is stale — the requirements changed since it was "
            "locked; rebuild the cell with media-runtime-build.yml"
        )
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise RenderError(f"{where}: the lock entry has no pack size")
    if packs_dir is None:
        return
    pack = packs_dir / entry["path"]
    if not pack.is_file():
        raise RenderError(f"{where}: the published pack is missing: {pack}")
    actual = pack.stat().st_size
    if actual != size:
        raise RenderError(f"{where}: {pack.name} is {actual} bytes, the lock says size {size}")
    if lock.sha256_file(pack) != sha256:
        raise RenderError(f"{where}: {pack.name} does not match the locked sha256")


def render_manifest(
    requirements: Mapping,
    lock_doc: Mapping,
    *,
    packs_dir: Path | None,
    downloads_base: str,
) -> dict:
    """The shipped manifest document: every declared cell, published iff locked."""
    if lock_doc and lock_doc.get("schemaVersion") != lock.LOCK_SCHEMA:
        raise RenderError(f"unsupported lock schemaVersion {lock_doc.get('schemaVersion')!r}")
    locked = dict(lock_doc.get("packs", {})) if lock_doc else {}
    declared = {f"{runtime}/{cell}" for runtime, cell in lock.runtime_cells(requirements)}
    for key, entry in locked.items():
        if key not in declared or key != f"{entry.get('runtime')}/{entry.get('cell')}":
            raise RenderError(f"the locked pack {key} is absent from the requirements")
    base = downloads_base.rstrip("/")
    runtimes: dict[str, dict] = {}
    for runtime, cell in lock.runtime_cells(requirements):
        spec = lock.cell_spec(requirements, runtime, cell)
        row = {
            "published": False,
            "url": "",
            "size": 0,
            "sha256": "",
            "archive": spec["archive"],
            "version": spec["version"],
            "license": spec["license"],
            "source": spec["source"],
        }
        entry = locked.get(f"{runtime}/{cell}")
        if entry is not None:
            _check_entry(requirements, spec, entry, packs_dir)
            row.update(
                published=True,
                url=f"{base}/{entry['path']}",
                size=entry["size"],
                sha256=entry["sha256"],
            )
        runtimes.setdefault(runtime, {"cells": {}})["cells"][cell] = row
    doc = {
        "schema": ai_models.MEDIA_RUNTIME_SCHEMA,
        "generatedFrom": GENERATED_FROM,
        "runtimes": runtimes,
    }
    try:
        ai_models.parse_media_runtime_manifest(doc)
    except ValueError as exc:
        raise RenderError(f"the rendered manifest is invalid: {exc}") from exc
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--requirements", type=Path, default=lock.DEFAULT_REQUIREMENTS)
    parser.add_argument(
        "--lock",
        type=Path,
        default=lock.DEFAULT_LOCK,
        help="pack lock written by lock_media_runtime.py (missing = nothing published)",
    )
    parser.add_argument(
        "--packs-dir",
        type=Path,
        default=None,
        help="published packs/ tree; every locked pack must be there byte-for-byte",
    )
    parser.add_argument(
        "--downloads-base",
        default=None,
        help="published base URL (default: the requirements' downloadsBase)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the rendered manifest differs from --out",
    )
    args = parser.parse_args(argv)

    requirements = lock.load_requirements(args.requirements)
    lock_doc = json.loads(args.lock.read_text(encoding="utf-8")) if args.lock.is_file() else {}
    doc = render_manifest(
        requirements,
        lock_doc,
        packs_dir=args.packs_dir,
        downloads_base=args.downloads_base or requirements["downloadsBase"],
    )
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if current != text:
            print(f"{args.out} is out of date — re-run without --check", file=sys.stderr)
            return 1
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RenderError, lock.LockError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
