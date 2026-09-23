"""Pinned, checksum+metadata-verified Qwen3-TTS GGUF model manifests.

The managed GGUF engine installs four talkers (base/customvoice ×
Q8_0/Q4_K_M) plus two tokenizers shared across profiles per quantization:

    <root>/<variant_key>/qwen-talker-0.6b-<profile>-<quant>.gguf
    <root>/shared/qwen-tokenizer-12hz-<quant>.gguf

Every file is pinned by size, SHA-256 *and* required GGUF metadata
(architecture, file type, model type) — a file whose name and bytes happen
to match but whose header declares the wrong model can never satisfy the
contract. The data ships inside the package and is rendered by
``scripts/fetch_qwen_gguf_models.py`` from the pinned HF revision.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

FORMAT_VERSION = "qwen-gguf-model-v1"
MANIFEST_PATH = Path(__file__).with_name("qwen_gguf_model_manifests.json")

PROFILE_KEYS = ("base", "customvoice")
QUANTIZATIONS = ("Q8_0", "Q4_K_M")

#: EngineId -> manifest profile key (the semantic names the GGUF repo uses).
PROFILE_KEY_FOR: Mapping[str, str] = {
    "qwen_base_0_6b": "base",
    "qwen_custom_0_6b": "customvoice",
}

TALKER_ARCHITECTURE = "qwen3-tts"
TOKENIZER_ARCHITECTURE = "qwen3-tts-tokenizer"
MODEL_TYPE_FOR: Mapping[str, str] = {"base": "base", "customvoice": "custom_voice"}

_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_VARIANT_KEY = re.compile(r"(base|customvoice)-(Q8_0|Q4_K_M)")

# 256 MiB headroom: re-install keeps the previous copy until promotion.
DOWNLOAD_HEADROOM_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class QwenGgufFile:
    """One pinned GGUF artifact: bytes + the header metadata it must declare."""

    path: str
    size_bytes: int
    sha256: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class QwenGgufModelManifest:
    format_version: str
    repo: str
    revision: str
    talkers: Mapping[str, QwenGgufFile]
    tokenizers: Mapping[str, QwenGgufFile]

    def recipe_for(self, profile: str, quantization: str) -> QwenGgufVariantRecipe | None:
        """The verified talker+tokenizer pair for one profile/quantization."""
        talker = self.talkers.get(f"{profile}-{quantization}")
        tokenizer = self.tokenizers.get(quantization)
        if talker is None or tokenizer is None:
            return None
        return QwenGgufVariantRecipe(
            profile=profile,
            quantization=quantization,
            talker=talker,
            tokenizer=tokenizer,
            repo=self.repo,
            revision=self.revision,
        )


@dataclass(frozen=True)
class QwenGgufVariantRecipe:
    """The install unit: one talker plus the tokenizer its quantization shares."""

    profile: str
    quantization: str
    talker: QwenGgufFile
    tokenizer: QwenGgufFile
    repo: str
    revision: str

    @property
    def key(self) -> str:
        return f"{self.profile}-{self.quantization}"

    @property
    def total_bytes(self) -> int:
        return self.talker.size_bytes + self.tokenizer.size_bytes

    @property
    def required_free_bytes(self) -> int:
        return self.total_bytes + DOWNLOAD_HEADROOM_BYTES

    @property
    def model_identity(self) -> str:
        """Content-addressed provenance stamped into synthesis contexts."""
        return (
            f"{self.repo}@{self.revision[:12]}:{self.key}:"
            f"{self.talker.sha256[:12]}+{self.tokenizer.sha256[:12]}"
        )


def _validate_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError(f"model path is unsafe: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError(f"model path is unsafe: {path!r}")


def _validate_file(record: QwenGgufFile, *, label: str) -> None:
    _validate_path(record.path)
    if not isinstance(record.size_bytes, int) or isinstance(record.size_bytes, bool):
        raise ValueError(f"GGUF file size is invalid: {record.path}")
    if record.size_bytes <= 0:
        raise ValueError(f"GGUF file size is invalid: {record.path}")
    if not _SHA256.fullmatch(record.sha256):
        raise ValueError(f"GGUF file SHA-256 is invalid: {record.path}")
    if not record.metadata.get("general.architecture"):
        raise ValueError(f"GGUF file declares no architecture: {record.path}")
    if not record.metadata.get("general.file_type"):
        raise ValueError(f"GGUF file declares no file type: {record.path}")
    for key, value in record.metadata.items():
        if not isinstance(key, str) or not isinstance(value, (str, int)):
            raise ValueError(f"GGUF metadata expectation is malformed: {label}")


def _validate_manifest(manifest: QwenGgufModelManifest) -> None:
    if not _REPO.fullmatch(manifest.repo):
        raise ValueError(f"model repository is not pinned to an owner/name: {manifest.repo}")
    if not _REVISION.fullmatch(manifest.revision):
        raise ValueError(f"model revision is not an immutable commit: {manifest.revision}")
    for key in PROFILE_KEYS:
        for quant in QUANTIZATIONS:
            recipe = manifest.recipe_for(key, quant)
            if recipe is None:
                raise ValueError(f"manifest is missing variant {key}-{quant}")
            talker = recipe.talker
            _validate_file(talker, label=recipe.key)
            if talker.metadata.get("general.architecture") != TALKER_ARCHITECTURE:
                raise ValueError(f"talker architecture must be {TALKER_ARCHITECTURE}: {recipe.key}")
            if talker.metadata.get("general.file_type") != quant:
                raise ValueError(f"talker file_type must equal {quant}: {recipe.key}")
            expected_type = MODEL_TYPE_FOR[key]
            if talker.metadata.get("qwen3-tts.model_type") != expected_type:
                raise ValueError(f"talker model_type must be {expected_type!r}: {recipe.key}")
    for quant in QUANTIZATIONS:
        tokenizer = manifest.tokenizers[quant]
        _validate_file(tokenizer, label=f"tokenizer-{quant}")
        if tokenizer.metadata.get("general.architecture") != TOKENIZER_ARCHITECTURE:
            raise ValueError(f"tokenizer architecture must be {TOKENIZER_ARCHITECTURE}: {quant}")
        if tokenizer.metadata.get("general.file_type") != quant:
            raise ValueError(f"tokenizer file_type must equal {quant}")
    paths = [f.path for f in (*manifest.talkers.values(), *manifest.tokenizers.values())]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate GGUF file paths in the manifest")


def _file_from(entry: object, *, label: str) -> QwenGgufFile:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{label} entry is malformed")
    metadata = entry.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{label} metadata is malformed")
    return QwenGgufFile(
        path=str(entry.get("path", "")),
        size_bytes=int(entry.get("sizeBytes", 0)),
        sha256=str(entry.get("sha256", "")),
        metadata={str(k): v for k, v in metadata.items()},
    )


def load_manifest_from_data(data: Mapping[str, object]) -> QwenGgufModelManifest:
    """Build and validate the manifest from a rendered data file."""
    if data.get("formatVersion") != FORMAT_VERSION:
        raise ValueError(f"unsupported GGUF manifest format: {data.get('formatVersion')!r}")
    talkers_raw = data.get("talkers")
    tokenizers_raw = data.get("tokenizers")
    if not isinstance(talkers_raw, Mapping) or not isinstance(tokenizers_raw, Mapping):
        raise ValueError("GGUF manifest is missing talkers/tokenizers")
    manifest = QwenGgufModelManifest(
        format_version=FORMAT_VERSION,
        repo=str(data.get("repo", "")),
        revision=str(data.get("revision", "")),
        talkers={str(key): _file_from(entry, label=str(key)) for key, entry in talkers_raw.items()},
        tokenizers={
            str(key): _file_from(entry, label=str(key)) for key, entry in tokenizers_raw.items()
        },
    )
    for key in manifest.talkers:
        if not _VARIANT_KEY.fullmatch(key):
            raise ValueError(f"unknown talker variant key: {key}")
    for key in manifest.tokenizers:
        if key not in QUANTIZATIONS:
            raise ValueError(f"unknown tokenizer quantization: {key}")
    _validate_manifest(manifest)
    return manifest


def _load_shipped_manifest(path: Path = MANIFEST_PATH) -> QwenGgufModelManifest | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    try:
        return load_manifest_from_data(data)
    except (ValueError, KeyError, TypeError):
        return None


MANIFEST: QwenGgufModelManifest | None = _load_shipped_manifest()


def recipe_for(profile: str, quantization: str) -> QwenGgufVariantRecipe | None:
    """The verified recipe for a manifest profile key and quantization."""
    return MANIFEST.recipe_for(profile, quantization) if MANIFEST is not None else None


def recipe_for_variant(variant: object) -> QwenGgufVariantRecipe | None:
    """Resolve a :class:`QwenVariant` to its recipe; None unless GGUF."""
    profile_key = PROFILE_KEY_FOR.get(getattr(variant, "profile", ""))
    if profile_key is None or getattr(variant, "model_format", "") != "gguf":
        return None
    return recipe_for(profile_key, str(getattr(variant, "quantization", "")))


# --- GGUF header parser -------------------------------------------------------

_GGUF_SCALARS = {
    0: ("B", 1),  # UINT8
    1: ("b", 1),  # INT8
    2: ("H", 2),  # UINT16
    3: ("h", 2),  # INT16
    4: ("I", 4),  # UINT32
    5: ("i", 4),  # INT32
    6: ("f", 4),  # FLOAT32
    7: ("?", 1),  # BOOL
    10: ("Q", 8),  # UINT64
    11: ("q", 8),  # INT64
    12: ("d", 8),  # FLOAT64
}
_GGUF_STRING = 8
_GGUF_ARRAY = 9
_MAX_STRING = 1 << 22
_MAX_KV = 4096
_MAX_ARRAY = 1 << 22


class GgufError(ValueError):
    """A file that is not a parseable GGUF, or lies about its layout."""


def parse_gguf_metadata(
    read: Callable[[int], bytes],
    wanted: frozenset[str] | None = None,
) -> dict[str, object]:
    """Decode the GGUF header KV section via ``read(n) -> exactly n bytes``.

    Stops as soon as every wanted key has been decoded (or the KV section
    ends), so callers can hand in a lazily-fetched stream — the parser never
    touches tensor data. Raises :class:`GgufError` on malformed input.
    """

    def take(n: int) -> bytes:
        try:
            data = read(n)
        except Exception as exc:  # noqa: BLE001 - normalize transport errors
            raise GgufError(f"GGUF header read failed: {exc}") from exc
        if len(data) != n:
            raise GgufError("GGUF header is truncated")
        return data

    def u32() -> int:
        return struct.unpack("<I", take(4))[0]

    def u64() -> int:
        return struct.unpack("<Q", take(8))[0]

    def string() -> str:
        n = u64()
        if n > _MAX_STRING:
            raise GgufError(f"GGUF string is unreasonably large: {n}")
        return take(n).decode("utf-8", "replace")

    def value(vtype: int) -> object:
        if vtype == _GGUF_STRING:
            return string()
        if vtype == _GGUF_ARRAY:
            elem_type = u32()
            count = u64()
            if count > _MAX_ARRAY:
                raise GgufError(f"GGUF array is unreasonably large: {count}")
            if elem_type == _GGUF_STRING:
                return [string() for _ in range(count)]
            fmt_size = _GGUF_SCALARS.get(elem_type)
            if fmt_size is None:
                raise GgufError(f"GGUF array element type is unsupported: {elem_type}")
            fmt, size = fmt_size
            return list(struct.unpack(f"<{count}{fmt}", take(count * size)))
        fmt_size = _GGUF_SCALARS.get(vtype)
        if fmt_size is None:
            raise GgufError(f"GGUF value type is unsupported: {vtype}")
        fmt, size = fmt_size
        return struct.unpack(f"<{fmt}", take(size))[0]

    if take(4) != b"GGUF":
        raise GgufError("not a GGUF file")
    version = u32()
    if version < 2 or version > 3:
        raise GgufError(f"unsupported GGUF version: {version}")
    take(8)  # n_tensors — not needed for verification
    n_kv = u64()
    if n_kv > _MAX_KV:
        raise GgufError(f"GGUF metadata count is unreasonably large: {n_kv}")
    metadata: dict[str, object] = {}
    for _ in range(n_kv):
        key = string()
        val = value(u32())
        metadata[key] = val
        if wanted is not None and wanted <= metadata.keys():
            break
    return metadata


def gguf_metadata_of(path: Path, wanted: frozenset[str] | None = None) -> dict[str, object]:
    """Read the GGUF header KV section of a local file."""
    with path.open("rb") as handle:

        def read(n: int) -> bytes:
            return handle.read(n)

        return parse_gguf_metadata(read, wanted)


def metadata_matches(record: QwenGgufFile, path: Path) -> str:
    """ "" when the file's GGUF metadata satisfies the record, else the reason."""
    try:
        metadata = gguf_metadata_of(path, frozenset(record.metadata))
    except (GgufError, OSError) as exc:
        return f"GGUF metadata is unreadable for {record.path}: {exc}"
    for key, expected in record.metadata.items():
        actual = metadata.get(key)
        if actual != expected:
            return (
                f"GGUF metadata mismatch for {record.path}: "
                f"{key} is {actual!r}, expected {expected!r}"
            )
    return ""
