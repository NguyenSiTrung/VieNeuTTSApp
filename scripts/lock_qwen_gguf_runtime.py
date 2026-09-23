#!/usr/bin/env python
"""Lock a built qwentts.cpp native pack into a digest/size manifest.

Maintainer tool — the application never runs this.  Given a staged pack
produced by ``scripts/build_qwen_gguf_runtime.py`` it emits the immutable
manifest Phase 2's installer verifies against: every regular file by path,
size and SHA-256, every symlink by path and target, plus the upstream build
identity (commits, ABI version, cell, deployment floor).

Manifests are merged into ``packaging/qwen-gguf-pack-manifests.json`` keyed by
cell; cells stay absent until their packs exist on real hardware — a missing
cell is an explicit blocker, not a failure of this tool.

Usage:
    python scripts/lock_qwen_gguf_runtime.py --pack <dir> --cell linux-x64-cpu
    python scripts/lock_qwen_gguf_runtime.py --pack <dir> --cell linux-x64-cpu --check
    python scripts/lock_qwen_gguf_runtime.py --pack <dir> --cell linux-x64-cpu --print
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "packaging" / "qwen-gguf-runtime-requirements.json"
DEFAULT_MANIFESTS = REPO_ROOT / "packaging" / "qwen-gguf-pack-manifests.json"
FORMAT_VERSION = "qwen-gguf-pack-v1"


class LockError(RuntimeError):
    """Raised when a pack cannot be locked or fails verification."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_requirements(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell_spec(requirements: dict, key: str) -> dict:
    for cell in requirements.get("cells", []):
        if cell.get("key") == key:
            return cell
    known = ", ".join(c["key"] for c in requirements.get("cells", []))
    raise LockError(f"unknown cell {key!r} — expected one of: {known}")


def pack_manifest(pack_dir: Path, spec: dict, upstream: dict) -> dict:
    """Digest/size manifest for a staged pack directory (deterministic)."""
    if not pack_dir.is_dir():
        raise LockError(f"pack dir {pack_dir} does not exist")
    files: list[dict] = []
    links: list[dict] = []
    for path in sorted(pack_dir.rglob("*")):
        rel = path.relative_to(pack_dir).as_posix()
        if path.is_symlink():
            links.append({"path": rel, "target": path.readlink().as_posix()})
        elif path.is_file():
            files.append({"path": rel, "size": path.stat().st_size, "sha256": _sha256(path)})
    return {
        "formatVersion": FORMAT_VERSION,
        "cell": spec["key"],
        "device": spec["device"],
        "ggmlBackend": spec["ggmlBackend"],
        "deploymentFloor": spec.get("deploymentFloor", ""),
        "abiVersion": upstream["abiVersion"],
        "upstream": {
            "repo": upstream["repo"],
            "commit": upstream["commit"],
            "ggmlSubmoduleRepo": upstream["ggmlSubmoduleRepo"],
            "ggmlSubmoduleCommit": upstream["ggmlSubmoduleCommit"],
            "header": upstream["header"],
        },
        "files": files,
        "links": links,
    }


def check_pack(pack_dir: Path, manifest: dict) -> list[str]:
    """Verify a pack dir against a manifest; returns problems (empty = ok)."""
    problems: list[str] = []
    if not pack_dir.is_dir():
        return [f"pack dir {pack_dir} does not exist"]
    for entry in manifest.get("files", []):
        path = pack_dir / entry["path"]
        if not path.is_file():
            problems.append(f"missing file: {entry['path']}")
            continue
        if path.stat().st_size != entry["size"]:
            problems.append(
                f"size mismatch: {entry['path']} is {path.stat().st_size}, expected {entry['size']}"
            )
            continue
        if _sha256(path) != entry["sha256"]:
            problems.append(f"sha256 mismatch: {entry['path']}")
    for link in manifest.get("links", []):
        path = pack_dir / link["path"]
        if not path.is_symlink():
            problems.append(f"missing symlink: {link['path']}")
        elif path.readlink().as_posix() != link["target"]:
            problems.append(
                f"symlink target mismatch: {link['path']} -> "
                f"{path.readlink().as_posix()}, expected {link['target']}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pack", type=Path, required=True, help="staged pack dir")
    parser.add_argument("--cell", required=True, help="matrix cell key")
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--manifests", type=Path, default=DEFAULT_MANIFESTS)
    parser.add_argument("--check", action="store_true", help="verify pack vs manifests")
    parser.add_argument("--print", dest="print_only", action="store_true")
    args = parser.parse_args(argv)

    requirements = _load_requirements(args.requirements)
    spec = _cell_spec(requirements, args.cell)

    if args.check:
        manifests = json.loads(args.manifests.read_text(encoding="utf-8"))
        manifest = manifests.get("packs", {}).get(args.cell)
        if manifest is None:
            raise LockError(f"no locked manifest for cell {args.cell}")
        problems = check_pack(args.pack, manifest)
        for p in problems:
            print(f"pack problem: {p}", file=sys.stderr)
        if problems:
            raise LockError(f"{args.cell} pack drifted ({len(problems)} problems)")
        print(f"{args.cell}: pack matches the locked manifest")
        return 0

    manifest = pack_manifest(args.pack, spec, requirements["upstream"]["runtime"])
    if args.print_only:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0

    manifests: dict = {"formatVersion": FORMAT_VERSION, "packs": {}}
    if args.manifests.is_file():
        manifests = json.loads(args.manifests.read_text(encoding="utf-8"))
    manifests.setdefault("packs", {})[args.cell] = manifest
    args.manifests.parent.mkdir(parents=True, exist_ok=True)
    args.manifests.write_text(
        json.dumps(manifests, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"locked {args.cell}: {len(manifest['files'])} files into {args.manifests}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
