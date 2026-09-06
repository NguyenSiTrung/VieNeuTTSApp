"""Throwaway CI probe for dynamic CUDA wheel activation.

Downloads happen in the workflow. This script extracts the resolved wheel
closure without pip, imports it from the target directory, and emits a small
JSON report. It is not shipped with the application.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path


def _extract_wheels(wheels_dir: Path, site_packages: Path) -> None:
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"no wheels found in {wheels_dir}")
    site_packages.mkdir(parents=True, exist_ok=True)
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site_packages)


def _wheel_records(wheels_dir: Path) -> list[dict[str, str | int]]:
    records = []
    for wheel in sorted(wheels_dir.glob("*.whl")):
        digest = hashlib.sha256()
        with wheel.open("rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                digest.update(block)
        records.append(
            {
                "filename": wheel.name,
                "sha256": digest.hexdigest(),
                "size_bytes": wheel.stat().st_size,
            }
        )
    return records


def _torch_cuda_library(torch_lib: Path) -> Path:
    patterns = ("c10_cuda.dll", "libc10_cuda.so")
    for name in patterns:
        candidate = torch_lib / name
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"no c10 CUDA library found in {torch_lib}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args(argv)

    site_packages = args.runtime / "site-packages"
    _extract_wheels(args.wheels, site_packages)
    torch_lib = site_packages / "torch" / "lib"
    if sys.platform == "win32":
        if not hasattr(os, "add_dll_directory"):
            raise RuntimeError("Windows DLL directory activation is unavailable")
        os.add_dll_directory(str(torch_lib))
    sys.path.insert(0, str(site_packages))

    import torch
    import torchaudio
    import transformers

    library = _torch_cuda_library(torch_lib)
    ctypes.CDLL(str(library))
    print(
        json.dumps(
            {
                "platform": sys.platform,
                "torch": torch.__version__,
                "torch_cuda": torch.version.cuda,
                "torchaudio": torchaudio.__version__,
                "transformers": transformers.__version__,
                "cuda_library": library.name,
                "wheels": _wheel_records(args.wheels),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
