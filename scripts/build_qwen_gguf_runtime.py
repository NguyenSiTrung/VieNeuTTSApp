#!/usr/bin/env python
"""Build a reproducible qwentts.cpp native runtime pack for one matrix cell.

Maintainer tool — the application never runs this.  It drives a pinned
clone+configure+build+stage pipeline for the shared-library pack described in
``packaging/qwen-gguf-runtime-requirements.json`` (track
``qwen_gguf_engine_20260923``, Task 1.2).  All command lists are produced by
pure functions so unit tests can pin the build contract without a toolchain.

Pack layout (flat, location-independent):

    libqwen.so | qwen.dll | libqwen.dylib     (SONAME/versioned names kept)
    libggml*/ggml* backend + cpu-variant modules
    licenses/{qwentts.cpp,ggml}-MIT.txt
    BUILD-INFO.json                            (cell, commits, flags, floor)

Two relocation contracts matter:

- ggml backend modules are discovered relative to the *process* cwd /
  executable dir — never the libqwen dir — so the host subprocess is spawned
  with ``cwd=<pack dir>`` (see ``backendDiscovery`` in the requirements).
- ``libqwen.so`` ships with an absolute build-tree RUNPATH; ``stage_pack``
  rewrites it to ``$ORIGIN`` (Linux) / ``@loader_path`` (macOS) so the pack
  resolves its ggml deps wherever it lands.  Windows resolves from the module
  directory natively.

Usage:
    python scripts/build_qwen_gguf_runtime.py --cell linux-x64-cpu \
        --source-dir /path/to/qwentts.cpp --out build/qwen-gguf-pack-linux-x64-cpu
    python scripts/build_qwen_gguf_runtime.py --cell linux-x64-cpu \
        --clone-dir /path/to/parent --out ...        # clone at the pinned commit
    python scripts/build_qwen_gguf_runtime.py --cell linux-x64-cpu --dry-run
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "packaging" / "qwen-gguf-runtime-requirements.json"
UPSTREAM_REPO_URL = "https://github.com/ServeurpersoCom/qwentts.cpp"

LICENSE_FILES = {
    "qwentts.cpp-MIT.txt": "LICENSE",
    "ggml-MIT.txt": "ggml/LICENSE",
}

# Linux packs carry the OpenMP runtime the ggml modules NEED.  Shipped under
# the GCC Runtime Library Exception; the notice is copied from the build
# host's gcc-*-base copyright file at stage time.
LINUX_GOMP = ("libgomp.so.1", "libgomp.so.1.0.0")


class BuildError(RuntimeError):
    """Raised for contract violations before or during pack production."""


def load_requirements(path: Path = DEFAULT_REQUIREMENTS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cell_spec(requirements: dict, key: str) -> dict:
    """Return the cell entry from the requirements manifest or raise."""
    for cell in requirements.get("cells", []):
        if cell.get("key") == key:
            spec = dict(cell)
            spec["upstream"] = requirements["upstream"]["runtime"]
            return spec
    known = ", ".join(c["key"] for c in requirements.get("cells", []))
    raise BuildError(f"unknown cell {key!r} — expected one of: {known}")


def clone_plan(upstream: dict, dest: Path) -> list[list[str]]:
    """git commands producing a checkout at the pinned commit + submodule."""
    commit = upstream["commit"]
    ggml_commit = upstream["ggmlSubmoduleCommit"]
    return [
        ["git", "clone", UPSTREAM_REPO_URL, str(dest)],
        ["git", "-C", str(dest), "checkout", commit],
        ["git", "-C", str(dest), "submodule", "update", "--init", "--recursive"],
        # The superproject records the submodule commit; checking it out
        # explicitly makes the pin auditable and catches .gitmodules drift.
        ["git", "-C", str(dest / "ggml"), "checkout", ggml_commit],
    ]


def _main_lib(spec: dict) -> str:
    return spec["upstream"]["sharedLibrary"]["names"][spec["os"]]


def configure_command(spec: dict, source_dir: Path, build_dir: Path) -> list[str]:
    cmd = [
        "cmake",
        "-S",
        str(source_dir),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    cmd.extend(spec["cmakeFlags"])
    if spec["os"] == "macos":
        cmd.append("-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0")
    if spec["os"] == "windows":
        cmd.extend(["-A", "x64"])
    return cmd


def build_command(spec: dict, build_dir: Path, jobs: int = 0) -> list[str]:
    cmd = ["cmake", "--build", str(build_dir), "--config", "Release"]
    if jobs:
        cmd.extend(["--parallel", str(jobs)])
    return cmd


def _lib_ext(spec: dict) -> str:
    return {"linux": "so", "windows": "dll", "macos": "dylib"}[spec["os"]]


def _stem(spec: dict) -> str:
    """Library filename stem: 'libggml' on unix, 'ggml' on Windows."""
    return "ggml" if spec["os"] == "windows" else "libggml"


def required_pack_files(spec: dict) -> list[str]:
    """Concrete files every pack for this cell must contain.

    Linux names are verified against the real build (SONAME-versioned ggml).
    Windows/macOS names are the canonical dev-link names; the per-cell glob
    rules in ``pack_globs`` cover versioned variants and stay the real gate
    until those packs are built and their evidence lands.
    """
    stem = _stem(spec)
    if spec["os"] == "linux":
        files = [
            "libqwen.so",
            "libggml.so.0",
            "libggml-base.so.0",
            "libggml-cpu-x64.so",
            "libgomp.so.1",
        ]
        if spec["device"] == "cuda":
            files.append("libggml-cuda.so")
        return files
    if spec["os"] == "windows":
        files = ["qwen.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu-x64.dll"]
        if spec["device"] == "cuda":
            files.append("ggml-cuda.dll")
        return files
    files = ["libqwen.dylib", f"{stem}.dylib", f"{stem}-base.dylib", f"{stem}-cpu.dylib"]
    if spec["device"] == "metal":
        files.append(f"{stem}-metal.dylib")
    return files


def pack_globs(spec: dict) -> list[dict]:
    """Glob rules verified against a staged pack (each needs >= min matches)."""
    ext = _lib_ext(spec)
    stem = _stem(spec)
    rules = [{"pattern": f"{stem}-cpu-*.{ext}", "min": 1, "why": "CPU ISA-variant backend modules"}]
    if spec["device"] == "cuda":
        rules.append({"pattern": f"{stem}-cuda*.{ext}", "min": 1, "why": "CUDA backend module"})
    if spec["device"] == "metal":
        rules.append({"pattern": f"{stem}-metal*.{ext}", "min": 1, "why": "Metal backend module"})
    return rules


def _max_glibc_from_objdump(text: str) -> str | None:
    """Highest GLIBC_x.y symbol version referenced in objdump -T output."""
    versions = re.findall(r"GLIBC_(\d+)\.(\d+)", text)
    if not versions:
        return None
    major, minor = max(versions, key=lambda v: (int(v[0]), int(v[1])))
    return f"{major}.{minor}"


def glibc_floor(pack_dir: Path) -> str | None:
    """Measure the pack's glibc floor from its own binaries (Linux only).

    The floor is whatever the build toolchain linked against — a pack built
    on Ubuntu 24.04 needs 2.38 even if CI targets 22.04's 2.35.  Measuring
    the binaries keeps BUILD-INFO honest instead of repeating the runner's
    promise.
    """
    best: str | None = None
    for lib in sorted(pack_dir.glob("*.so*")):
        if lib.is_symlink() or not lib.is_file():
            continue
        try:
            out = subprocess.run(
                ["objdump", "-T", str(lib)],
                check=False,
                capture_output=True,
                text=True,
            ).stdout
        except OSError:
            continue
        found = _max_glibc_from_objdump(out)
        if found is not None and (
            best is None
            or tuple(int(x) for x in found.split(".")) > tuple(int(x) for x in best.split("."))
        ):
            best = found
    return best


def _find_gomp() -> Path | None:
    """Locate the build host's libgomp.so.1 for bundling (Linux packs)."""
    for candidate in (
        Path("/usr/lib/x86_64-linux-gnu/libgomp.so.1"),
        Path("/usr/lib64/libgomp.so.1"),
        Path("/lib/x86_64-linux-gnu/libgomp.so.1"),
    ):
        if candidate.exists():
            return candidate
    try:
        out = subprocess.run(
            ["gcc", "-print-file-name=libgomp.so.1"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out and Path(out).exists():
            return Path(out)
    except OSError:
        pass
    return None


def verify_pack(pack_dir: Path, spec: dict) -> list[str]:
    """Return problems with a staged pack; empty means it satisfies inventory."""
    problems: list[str] = []
    if not pack_dir.is_dir():
        return [f"pack dir {pack_dir} does not exist"]
    for rel in required_pack_files(spec):
        if not (pack_dir / rel).exists():
            problems.append(f"missing required file: {rel}")
    for rule in pack_globs(spec):
        matches = [p for p in pack_dir.iterdir() if fnmatch.fnmatch(p.name, rule["pattern"])]
        if len(matches) < rule["min"]:
            problems.append(
                f"missing {rule['why']}: no file matches {rule['pattern']} (need >= {rule['min']})"
            )
    licenses = pack_dir / "licenses"
    if not licenses.is_dir() or not any(licenses.iterdir()):
        problems.append("missing license notices: licenses/ is absent or empty")
    return problems


def fixup_rpath_command(spec: dict, pack_dir: Path) -> list[str] | None:
    """Make the pack location-independent ($ORIGIN / @loader_path)."""
    if spec["os"] == "linux":
        libs = [
            str(p) for p in sorted(pack_dir.iterdir()) if p.name.endswith(".so") or ".so." in p.name
        ]
        return ["patchelf", "--set-rpath", "$ORIGIN", *libs]
    if spec["os"] == "macos":
        cmds: list[str] = []
        for lib in sorted(pack_dir.glob("*.dylib")):
            cmds.append(f"install_name_tool -add_rpath @loader_path {lib}")
        return ["sh", "-c", " && ".join(cmds)]
    return None  # Windows resolves DLLs from the module directory


def stage_pack(
    spec: dict,
    build_dir: Path,
    pack_dir: Path,
    upstream: dict,
) -> list[Path]:
    """Copy the runtime files + notices from a build tree into pack_dir."""
    ext = _lib_ext(spec)
    stem = _stem(spec)
    pack_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []

    patterns = [f"*qwen*.{ext}", f"{stem}*.{ext}", f"{stem}*.{ext}.*"]
    seen: set[Path] = set()
    for pattern in patterns:
        for src in sorted(build_dir.glob(pattern)):
            if src in seen or src.is_dir():
                continue
            seen.add(src)
            dest = pack_dir / src.name
            if src.is_symlink():
                target = os.readlink(src)
                if dest.exists() or dest.is_symlink():
                    dest.unlink()
                os.symlink(target, dest)
            else:
                shutil.copy2(src, dest)
            staged.append(dest)

    lic_dir = pack_dir / "licenses"
    lic_dir.mkdir(exist_ok=True)
    source_root = spec.get("_source_dir", build_dir.parent)
    for name, rel in LICENSE_FILES.items():
        src = Path(source_root) / rel
        if src.is_file():
            shutil.copy2(src, lic_dir / name)
            staged.append(lic_dir / name)

    if spec["os"] == "linux":
        gomp = _find_gomp()
        if gomp is not None:
            real = gomp.resolve()
            shutil.copy2(real, pack_dir / LINUX_GOMP[1])
            staged.append(pack_dir / LINUX_GOMP[1])
            link = pack_dir / LINUX_GOMP[0]
            if link.exists() or link.is_symlink():
                link.unlink()
            os.symlink(LINUX_GOMP[1], link)
            staged.append(link)
            for notice in sorted(Path("/usr/share/doc").glob("gcc-*-base/copyright")):
                shutil.copy2(notice, lic_dir / "gcc-runtime-copyright.txt")
                staged.append(lic_dir / "gcc-runtime-copyright.txt")
                break

    build_info = {
        "cell": spec["key"],
        "upstream": {
            "repo": upstream["repo"],
            "commit": upstream["commit"],
            "ggmlSubmoduleRepo": upstream["ggmlSubmoduleRepo"],
            "ggmlSubmoduleCommit": upstream["ggmlSubmoduleCommit"],
        },
        "abiVersion": upstream["abiVersion"],
        "cmakeFlags": spec["cmakeFlags"],
        "device": spec["device"],
        "ggmlBackend": spec["ggmlBackend"],
        "deploymentFloor": spec.get("deploymentFloor", ""),
        "backendDiscovery": "host subprocess must run with cwd=<pack dir>",
    }
    if spec["os"] == "linux":
        floor = glibc_floor(pack_dir)
        if floor is not None:
            build_info["measuredGlibcFloor"] = f"glibc >= {floor} (measured from pack binaries)"
    info_path = pack_dir / "BUILD-INFO.json"
    info_path.write_text(json.dumps(build_info, indent=2, ensure_ascii=False) + "\n")
    staged.append(info_path)
    return staged


def symbol_smoke_command(spec: dict, pack_dir: Path) -> list[str]:
    """ctypes load of the pack's main lib + qt_version — no model, no backend."""
    code = (
        "import ctypes, sys\n"
        "lib = ctypes.CDLL(sys.argv[1])\n"
        "lib.qt_version.restype = ctypes.c_char_p\n"
        "print(lib.qt_version().decode())\n"
    )
    return [sys.executable, "-c", code, str(pack_dir / _main_lib(spec))]


def _run(cmd: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> None:
    print("+", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, cwd=cwd, check=True)


def _rev_parse(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cell", required=True, help="matrix cell key")
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--source-dir", type=Path, help="existing qwentts.cpp clone")
    src.add_argument("--clone-dir", type=Path, help="parent dir to clone into")
    parser.add_argument("--out", type=Path, required=True, help="pack output dir")
    parser.add_argument("--build-dir", type=Path, default=None)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)

    requirements = load_requirements(args.requirements)
    spec = cell_spec(requirements, args.cell)
    upstream = requirements["upstream"]["runtime"]

    if args.clone_dir is not None:
        source_dir = Path(args.clone_dir) / "qwentts.cpp"
        plan = clone_plan(upstream, source_dir)
        for cmd in plan:
            _run(cmd, dry_run=args.dry_run)
    else:
        source_dir = Path(args.source_dir)

    if not args.dry_run:
        if not (source_dir / "src" / "qwen.h").is_file():
            raise BuildError(f"{source_dir} is not a qwentts.cpp checkout (no src/qwen.h)")
        head = _rev_parse(source_dir)
        if head != upstream["commit"]:
            raise BuildError(
                f"source HEAD {head[:12]} != pinned commit {upstream['commit'][:12]} — "
                "checkout the pinned commit first"
            )
        ggml_head = _rev_parse(source_dir / "ggml")
        if ggml_head != upstream["ggmlSubmoduleCommit"]:
            raise BuildError(
                f"ggml submodule HEAD {ggml_head[:12]} != pinned "
                f"{upstream['ggmlSubmoduleCommit'][:12]}"
            )

    build_dir = args.build_dir or (Path(tempfile.gettempdir()) / f"qwen-gguf-build-{args.cell}")
    spec["_source_dir"] = source_dir

    _run(configure_command(spec, source_dir, build_dir), dry_run=args.dry_run)
    _run(build_command(spec, build_dir, args.jobs), dry_run=args.dry_run)

    if args.dry_run:
        print(f"would stage into {args.out}: {', '.join(required_pack_files(spec))}")
        for rule in pack_globs(spec):
            print(f"  glob {rule['pattern']} >= {rule['min']} ({rule['why']})")
        print("  licenses/: " + ", ".join(LICENSE_FILES))
        print("  BUILD-INFO.json")
        return 0

    staged = stage_pack(spec, build_dir, args.out, upstream)
    problems = verify_pack(args.out, spec)
    if problems:
        for p in problems:
            print(f"pack problem: {p}", file=sys.stderr)
        raise BuildError(f"pack verification failed ({len(problems)} problems)")

    fixup = fixup_rpath_command(spec, args.out)
    if fixup is not None:
        _run(fixup)

    print(f"staged {len(staged)} files into {args.out}")
    print("smoke:", " ".join(symbol_smoke_command(spec, args.out)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
