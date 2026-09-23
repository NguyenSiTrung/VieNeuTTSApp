#!/usr/bin/env python3
"""Regenerate shipped ``qwen_gguf_runtime_manifests.json`` cell entries.

The shipped manifest is generated — never hand-edited.  For each requested
cell this script merges three sources:

* ``packaging/qwen-gguf-runtime-requirements.json`` — device, ggml backend
  name, deployment floor, dependency list, pinned upstream commits/ABI.
* ``packaging/qwen-gguf-pack-manifests.json`` — the locked ``files``/``links``
  digest records produced by ``lock_qwen_gguf_runtime.py``.
* The built pack directory itself — the ``backends`` list is derived from the
  backend modules actually staged in the pack (``ggml-cpu-*`` → ``CPU``,
  ``ggml-metal`` → ``MTL0`` …) so the manifest can only advertise backends the
  bytes on disk really provide.

``downloads`` is set to ``<downloads-base>/<cell>`` when a base URL is given;
without one the existing list is preserved (empty for unpublished cells).
Cells not requested keep their existing entries untouched.

Used locally by maintainers and by the ``publish`` job of
``qwen-gguf-runtime-build.yml`` after CI-built packs are re-locked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "packaging" / "qwen-gguf-runtime-requirements.json"
DEFAULT_PACK_MANIFESTS = REPO_ROOT / "packaging" / "qwen-gguf-pack-manifests.json"
DEFAULT_OUT = REPO_ROOT / "src" / "vienetts_app" / "core" / "qwen_gguf_runtime_manifests.json"

GENERATED_FROM = (
    "packaging/qwen-gguf-pack-manifests.json (locked packs) + "
    "packaging/qwen-gguf-runtime-requirements.json (cell contract), rendered by "
    "scripts/render_qwen_gguf_runtime_manifests.py — only cells that were built, "
    "locked and published ship an install recipe"
)

# Backend-module filename -> device name comes from the requirements
# deviceSelection.names table (cpu->CPU, metal->MTL0, blas->BLAS, ...).
_MODULE_RE = re.compile(r"ggml-([a-z0-9]+)[-.]")


class RenderError(RuntimeError):
    """Raised when a cell cannot be rendered from the locked inputs."""


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _backend_names(requirements: dict) -> dict[str, str]:
    return dict(requirements["upstream"]["runtime"]["deviceSelection"]["names"])


def _derive_backends(pack_dir: Path, names: dict[str, str], primary: str) -> list[str]:
    """Map the pack's ggml backend modules to device names, primary first."""
    found: set[str] = set()
    for path in sorted(pack_dir.iterdir()):
        match = _MODULE_RE.search(path.name)
        if match is None:
            continue
        token = match.group(1)
        if token == "base":
            continue  # libggml-base is the shared core, not a loadable backend
        if token not in names:
            raise RenderError(
                f"unmapped backend module {path.name!r} in {pack_dir} — "
                f"extend upstream.runtime.deviceSelection.names"
            )
        found.add(names[token])
    if not found:
        raise RenderError(f"no ggml backend modules found in {pack_dir}")
    ordered = [primary] if primary in found else []
    ordered += [name for name in names.values() if name in found and name != primary]
    unknown = found - set(ordered)
    ordered += sorted(unknown)
    return ordered


def _cell_spec(requirements: dict, key: str) -> dict:
    for cell in requirements.get("cells", []):
        if cell.get("key") == key:
            return cell
    raise RenderError(f"cell {key!r} missing from requirements")


def render_cell(
    key: str,
    requirements: dict,
    locked_packs: dict,
    packs_dir: Path,
    existing: dict,
    downloads_base: str | None,
) -> dict:
    pack_dir = packs_dir / key
    if not pack_dir.is_dir():
        raise RenderError(f"pack directory missing for cell {key!r}: {pack_dir}")
    locked = locked_packs.get(key)
    if locked is None:
        raise RenderError(f"cell {key!r} has no locked pack entry — run lock_qwen_gguf_runtime.py first")

    cell = _cell_spec(requirements, key)
    runtime = requirements["upstream"]["runtime"]
    deps = cell.get("dependencies")
    if not deps:
        raise RenderError(f"cell {key!r} declares no dependencies in requirements")

    downloads = existing.get("downloads", [])
    if downloads_base:
        downloads = [f"{downloads_base.rstrip('/')}/{key}"]

    return {
        "cell": key,
        "device": cell["device"],
        "ggmlBackend": cell["ggmlBackend"],
        "abiVersion": runtime["abiVersion"],
        "library": runtime["sharedLibrary"]["names"][cell["os"]],
        "upstream": {
            "repo": runtime["repo"],
            "commit": runtime["commit"],
            "ggmlSubmoduleCommit": runtime["ggmlSubmoduleCommit"],
        },
        "deploymentFloor": cell["deploymentFloor"],
        "backends": _derive_backends(pack_dir, _backend_names(requirements), cell["ggmlBackend"]),
        "dependencies": list(deps),
        "files": locked["files"],
        "links": locked["links"],
        "downloads": downloads,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--pack-manifests", type=Path, default=DEFAULT_PACK_MANIFESTS,
                        help="locked pack manifests produced by lock_qwen_gguf_runtime.py")
    parser.add_argument("--packs-dir", type=Path, required=True,
                        help="directory containing one built pack directory per cell")
    parser.add_argument("--cells", type=str, default=None,
                        help="comma-separated cells to render; default: every locked cell with a pack dir")
    parser.add_argument("--downloads-base", type=str, default=None,
                        help="published base URL; per-cell download entry becomes <base>/<cell>")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit 1 if the rendered manifest differs from --out")
    args = parser.parse_args(argv)

    requirements = _load_json(args.requirements)
    locked_packs = _load_json(args.pack_manifests)["packs"]

    if args.cells:
        cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    else:
        cells = sorted(
            key for key in locked_packs
            if (args.packs_dir / key).is_dir()
        )
    if not cells:
        raise RenderError("no cells to render")

    if args.out.is_file():
        manifest = _load_json(args.out)
    else:
        manifest = {}
    manifest["formatVersion"] = "qwen-gguf-runtime-v1"
    manifest["generatedFrom"] = GENERATED_FROM
    manifest.setdefault("cells", {})

    for key in cells:
        existing = manifest["cells"].get(key, {})
        manifest["cells"][key] = render_cell(
            key, requirements, locked_packs, args.packs_dir, existing, args.downloads_base,
        )
        print(f"rendered {key}: {len(manifest['cells'][key]['files'])} files, "
              f"backends={manifest['cells'][key]['backends']}, "
              f"downloads={manifest['cells'][key]['downloads']}")

    rendered = json.dumps(manifest, indent=1) + "\n"
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if current != rendered:
            print(f"manifest drift: {args.out} differs from rendered output", file=sys.stderr)
            return 1
        print("manifest matches rendered output")
        return 0
    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
