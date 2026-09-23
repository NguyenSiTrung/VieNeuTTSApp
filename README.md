# VieNeuTTSApp runtime packs

Checksum-locked `qwentts.cpp` native runtime packs served for the app's
managed installer. Files are fetched one-by-one at
`packs/<cell>/<path>` and verified byte-for-byte against the digests in
`packaging/qwen-gguf-pack-manifests.json` on `main` — a file that differs
from the locked record fails verification and is never installed.

Symlinks are intentionally absent: the installer recreates them locally
from the manifest's `links` records.

Do not edit files in place — publish only through the pack pipeline
(build → lock → ship), so content and digests can never drift.
