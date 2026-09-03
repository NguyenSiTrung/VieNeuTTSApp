#!/usr/bin/env python
"""Download the minimal offline model bundle for the CPU (ONNX int8) build.

Downloads the pinned file set from Hugging Face into a plain directory layout
and writes a SHA256 ``manifest.json``; ``--verify`` re-checks an existing
bundle against the manifest.

Layout (matches the SDK's local-dir loading: backbone_repo=<out>/backbone,
onnx_dir=<out>/backbone/onnx_int8):

    models/
    ├── backbone/               # pnnbao-ump/VieNeu-TTS-v3-Turbo (CPU subset)
    │   ├── config.json
    │   ├── denoiser.onnx
    │   ├── speaker_encoder.onnx
    │   └── onnx_int8/...
    ├── codec/                  # OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX
    └── manifest.json

Note (spike §6): the SDK loads the codec via the HF cache even when
``onnx_dir`` is local, so packaging (Phase 5) either pre-seeds an HF cache
from ``models/codec`` or sets HF_HOME to a bundled cache. fp32 graphs
(``onnx_update/``) are opt-in via ``--precision fp32`` and add ~455 MB.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST

BACKBONE_REPO = OFFICIAL_MODEL_MANIFEST.backbone_repo
CODEC_REPO = OFFICIAL_MODEL_MANIFEST.codec_repo
BACKBONE_REVISION = OFFICIAL_MODEL_MANIFEST.backbone_revision
CODEC_REVISION = OFFICIAL_MODEL_MANIFEST.codec_revision

MINIMAL_BACKBONE = [item.relative_path for item in OFFICIAL_MODEL_MANIFEST.files_for("backbone")]
MINIMAL_CODEC = [
    item.relative_path for item in OFFICIAL_MODEL_MANIFEST.files_for("codec")
]  # exact set measured in the Phase 0 spike


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def bundle_files(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and ".cache" not in p.parts and p.name != "manifest.json"
    )


def build_manifest(root: Path, backbone: str = BACKBONE_REPO) -> dict:
    files = bundle_files(root)
    is_official = backbone == OFFICIAL_MODEL_MANIFEST.backbone_repo
    return {
        "format": OFFICIAL_MODEL_MANIFEST.format_version if is_official else "custom",
        "backbone_revision": BACKBONE_REVISION if is_official else None,
        "codec_revision": CODEC_REVISION,
        "repos": {"backbone": backbone, "codec": CODEC_REPO},
        "files": {rel: sha256(root / rel) for rel in files},
    }


def backbone_patterns(precision: str) -> list[str]:
    sub = "onnx_int8" if precision == "int8" else "onnx_update"
    roots = [p for p in MINIMAL_BACKBONE if "/" not in p]
    graphs = [f"{sub}/{p.split('/', 1)[1]}" for p in MINIMAL_BACKBONE if p.startswith("onnx_int8/")]
    return roots + graphs


def fetch(out: Path, precision: str, backbone: str = BACKBONE_REPO) -> dict:
    if backbone == OFFICIAL_MODEL_MANIFEST.backbone_repo:
        snapshot_download(
            backbone,
            revision=BACKBONE_REVISION,
            local_dir=str(out / "backbone"),
            allow_patterns=backbone_patterns(precision),
        )
    else:
        snapshot_download(
            backbone, local_dir=str(out / "backbone"), allow_patterns=backbone_patterns(precision)
        )
    snapshot_download(
        CODEC_REPO,
        revision=CODEC_REVISION,
        local_dir=str(out / "codec"),
        allow_patterns=MINIMAL_CODEC,
    )
    manifest = build_manifest(out, backbone)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def verify(out: Path) -> int:
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        print(f"no manifest at {manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = [
        (rel, "missing") if not (out / rel).is_file() else (rel, sha256(out / rel))
        for rel in manifest["files"]
        if not (out / rel).is_file() or sha256(out / rel) != manifest["files"][rel]
    ]
    for rel, why in bad:
        print(f"MISMATCH {rel}: {why}")
    print(f"verify: {len(manifest['files']) - len(bad)}/{len(manifest['files'])} files OK")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="models", help="bundle root (default: ./models)")
    ap.add_argument("--precision", choices=["int8", "fp32"], default="int8")
    ap.add_argument(
        "--backbone",
        default=BACKBONE_REPO,
        help="backbone HF repo id (default: the official VieNeu-TTS v3 Turbo repo)",
    )
    ap.add_argument("--verify", action="store_true", help="verify existing bundle against manifest")
    args = ap.parse_args(argv)

    out = Path(args.out)
    if args.verify:
        return verify(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = fetch(out, args.precision, backbone=args.backbone)
    total = sum((out / rel).stat().st_size for rel in manifest["files"])
    print(f"fetched {len(manifest['files'])} files, {total / 1e6:.0f} MB -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
