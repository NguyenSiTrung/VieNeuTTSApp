"""Official baseline model manifest (Phase 1 Task 1).

Immutable allowlist for the CPU ONNX-int8 baseline: repository revisions,
relative paths, expected byte sizes, and SHA-256 values. No Qt, no Hub import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OFFICIAL_MODEL_FORMAT = "official-v1"

RepoKey = Literal["backbone", "codec"]

DOWNLOAD_HEADROOM_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ModelFile:
    repo_key: RepoKey
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class OfficialModelManifest:
    format_version: str
    backbone_repo: str
    backbone_revision: str
    codec_repo: str
    codec_revision: str
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    @property
    def required_free_bytes(self) -> int:
        return self.total_bytes + DOWNLOAD_HEADROOM_BYTES

    def files_for(self, repo_key: RepoKey) -> tuple[ModelFile, ...]:
        return tuple(item for item in self.files if item.repo_key == repo_key)


OFFICIAL_MODEL_MANIFEST = OfficialModelManifest(
    format_version=OFFICIAL_MODEL_FORMAT,
    backbone_repo="pnnbao-ump/VieNeu-TTS-v3-Turbo",
    backbone_revision="2da0efab622a1722125991736524f080b751ef5b",
    codec_repo="OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX",
    codec_revision="ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae",
    files=(
        ModelFile(
            "backbone",
            "config.json",
            1553,
            "eee8e032cb936a60312f594a8156c086173a9c0255a545bd11a448f22a7c77ae",
        ),
        ModelFile(
            "backbone",
            "denoiser.onnx",
            42661414,
            "b7621953291cfe05e695a9c0ff4255aa2f93239fc17c26627e18b7b6b8f72f0b",
        ),
        ModelFile(
            "backbone",
            "speaker_encoder.onnx",
            28303423,
            "a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73",
        ),
        ModelFile(
            "backbone",
            "onnx_int8/config.json",
            2152,
            "a9f8d9c4b4736448ab355d1a98cfe48f5e39aecf2916c37b0806c228612e9a2d",
        ),
        ModelFile(
            "backbone",
            "onnx_int8/tokenizer.json",
            22320,
            "6cc6bcbe380b8c37bd9f2514e37c5dfa3e00e122c6e3125dae5c4afe48e39158",
        ),
        ModelFile(
            "backbone",
            "onnx_int8/vieneu_acoustic_cached.onnx",
            7207223,
            "8f2d7306a35c6128793838f39c4c2da2c176e243bd63f0963c56bbf0376c3939",
        ),
        ModelFile(
            "backbone",
            "onnx_int8/vieneu_backbone_shared.data",
            103891968,
            "429bfddd585b7a1907c7c9c944b3d91bc4da8b91f1f9982353351357140fd08f",
        ),
        ModelFile(
            "backbone",
            "onnx_int8/vieneu_prefill.onnx",
            1090823,
            "bc45488bd7802cd0e5d65cc427e9124e1a15b8b7e9fd86d37a3d16f1e847de4d",
        ),
        ModelFile(
            "backbone",
            "onnx_int8/vieneu_decode_step.onnx",
            1062040,
            "8346ce8fefa3635a2dcd29b6f8a5cb23c7acfd5da9dfad54090b0f9b797c4b5a",
        ),
        ModelFile(
            "backbone",
            "onnx_int8/vieneu_v3_heads.npz",
            52219622,
            "19ee6dd56530d7842c81fbd855f3d89440e2c3121e11f7e6ced447a559da585a",
        ),
        ModelFile(
            "codec",
            "codec_browser_onnx_meta.json",
            17036,
            "3e291c883bb7d11ff2fe8e964e3e495519760358859f35c951254c7741592731",
        ),
        ModelFile(
            "codec",
            "moss_audio_tokenizer_decode_full.onnx",
            681902,
            "0fbbafe3fd4afa2a019af5c5ced204af6e2d1db044fa40f021525d2aee95b4ac",
        ),
        ModelFile(
            "codec",
            "moss_audio_tokenizer_decode_shared.data",
            44198912,
            "e69d52e0f4e84ca27850557ee54face46632d3a5a16c89bd246c7c408466dcad",
        ),
        ModelFile(
            "codec",
            "moss_audio_tokenizer_decode_step.onnx",
            351400,
            "9527c86a29e1837edec1f74db57d5eeaadb3a715af3382703566460afed25855",
        ),
        ModelFile(
            "codec",
            "moss_audio_tokenizer_encode.data",
            44507136,
            "aa751265b2bab2887eac224484546b194875aa7494b607115439b3dc6b228a2c",
        ),
        ModelFile(
            "codec",
            "moss_audio_tokenizer_encode.onnx",
            815775,
            "eadea4a645abdcf98714c7aead122ee2ce7da6e080f9f80b977cd1ca8e19473a",
        ),
    ),
)
