#!/usr/bin/env python
"""Download the optional Qwen3-TTS model pack for offline use.

Snapshots both official 0.6B checkpoints into the Hugging Face hub cache
(the layout ``Qwen3TTSModel.from_pretrained`` reads directly), so a machine
without network can run Qwen CustomVoice/Base after one online fetch::

    python scripts/fetch_qwen_models.py
    HF_HOME=/path/to/offline/pack python scripts/fetch_qwen_models.py

Repos (pinned revisions print at the end; pass --revision to override):
- Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice (fixed multilingual speakers)
- Qwen/Qwen3-TTS-12Hz-0.6B-Base (reference-audio cloning)

The runtime itself stays optional: `pip install "vienetts-app[qwen]"`
(qwen-tts + pinned torch/torchaudio/transformers). The default install
is torch-free and unaffected. ~0.6B weights per checkpoint; CUDA needs
~4 GB VRAM, CPU fallback is slow — see docs/qwen-setup.md.
"""

from __future__ import annotations

import argparse
import sys

REPOS = (
    "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
)


def fetch(repo: str, revision: str | None) -> str:
    from huggingface_hub import snapshot_download

    path = snapshot_download(repo_id=repo, revision=revision)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch the optional Qwen3-TTS model pack.")
    parser.add_argument(
        "--revision",
        default=None,
        help="Single HF revision for both repos (default: each repo's default branch).",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Fetch only this repo (repeatable; defaults to both official checkpoints).",
    )
    args = parser.parse_args(argv)
    repos = args.repo or list(REPOS)
    for repo in repos:
        print(f"fetching {repo} ...", flush=True)
        try:
            path = fetch(repo, args.revision)
        except Exception as exc:  # noqa: BLE001 - report, keep going for the other repo
            print(f"FAILED {repo}: {exc}", file=sys.stderr)
            continue
        print(f"cached {repo} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
