"""Pure-Python ID3v2.4.0 tag builder for exported audiobook MP3 chapters.

Zero third-party dependencies: encodes standard UTF-8 (``0x03``) text frames,
``TXXX:narrator``, and front-cover ``APIC`` frames with strict ID3v2.4.0
28-bit synchsafe header and frame sizes so the resulting bytes can be prepended
directly ahead of a libsndfile-encoded MPEG stream.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Maximum payload bytes for any single text or ``TXXX`` frame (including encoding prefix).
MAX_TEXT_FRAME_BYTES = 64 * 1024

#: Maximum raw image bytes accepted for an ``APIC`` front-cover frame.
MAX_COVER_BYTES = 32 * 1024 * 1024

#: Threshold at or below which cover art is embedded on every track (above: track 1 only).
EMBED_ALL_TRACKS_COVER_LIMIT = 512 * 1024

_ID3V24_HEADER_PREFIX = b"ID3\x04\x00\x00"
_FRAME_FLAGS = b"\x00\x00"
_UTF8_ENCODING = b"\x03"
_APIC_FRONT_COVER = b"\x03"
_MAX_SYNCHSAFE = (1 << 28) - 1

_EXT_TO_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
_SUPPORTED_COVER_MIMES = frozenset(_EXT_TO_MIME.values())

__all__ = [
    "EMBED_ALL_TRACKS_COVER_LIMIT",
    "Id3Metadata",
    "MAX_COVER_BYTES",
    "MAX_TEXT_FRAME_BYTES",
    "build_id3v24_tag",
    "encode_synchsafe",
    "mime_for_image_ext",
    "should_embed_cover",
    "sniff_image_mime",
]


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def mime_for_image_ext(ext: str) -> str:
    """Map an image file extension (with optional leading dot) to its MIME type."""
    normalized = (ext or "").strip().lower()
    if normalized.startswith("."):
        normalized = normalized[1:]
    return _EXT_TO_MIME.get(normalized, "")


def sniff_image_mime(data: bytes) -> str:
    """Detect supported cover-image MIME type from raw magic bytes."""
    if not data:
        return ""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 6 and data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def should_embed_cover(cover_bytes: bytes | None, track_number: int) -> bool:
    """Return whether ``cover_bytes`` should be embedded on ``track_number``.

    Valid covers up to :data:`EMBED_ALL_TRACKS_COVER_LIMIT` embed on every track;
    larger covers (up to :data:`MAX_COVER_BYTES`) embed on track 1 only.
    """
    if not cover_bytes or len(cover_bytes) > MAX_COVER_BYTES:
        return False
    if not sniff_image_mime(cover_bytes):
        return False
    return track_number <= 1 or len(cover_bytes) <= EMBED_ALL_TRACKS_COVER_LIMIT


def encode_synchsafe(value: int) -> bytes:
    """Encode an unsigned 28-bit integer as 4 big-endian ID3 synchsafe bytes."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_SYNCHSAFE:
        raise ValueError(f"synchsafe integer must be in 0..{_MAX_SYNCHSAFE}, got {value!r}")
    return bytes(
        (
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        )
    )


def _truncate_utf8(text: str, max_bytes: int) -> bytes:
    """Encode ``text`` as UTF-8, truncating cleanly on a codepoint boundary."""
    if max_bytes <= 0:
        return b""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return raw
    cut = max_bytes
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    return raw[:cut]


def _build_frame(frame_id: bytes, payload: bytes) -> bytes:
    return frame_id + encode_synchsafe(len(payload)) + _FRAME_FLAGS + payload


def _build_text_frame(frame_id: bytes, text: str) -> bytes:
    payload = _UTF8_ENCODING + _truncate_utf8(text, MAX_TEXT_FRAME_BYTES - 1)
    return _build_frame(frame_id, payload)


def _build_txxx_frame(description: str, value: str) -> bytes:
    prefix = _UTF8_ENCODING + description.encode("utf-8") + b"\x00"
    payload = prefix + _truncate_utf8(value, MAX_TEXT_FRAME_BYTES - len(prefix))
    return _build_frame(b"TXXX", payload)


def _build_apic_frame(cover_bytes: bytes | None, cover_mime: str) -> bytes:
    if not cover_bytes or len(cover_bytes) > MAX_COVER_BYTES:
        return b""
    mime = cover_mime.strip().lower()
    if not mime:
        mime = sniff_image_mime(cover_bytes)
    if mime not in _SUPPORTED_COVER_MIMES:
        return b""
    payload = (
        _UTF8_ENCODING + mime.encode("ascii") + b"\x00" + _APIC_FRONT_COVER + b"\x00" + cover_bytes
    )
    return _build_frame(b"APIC", payload)


@dataclass(frozen=True)
class Id3Metadata:
    """Metadata fields serialized into an ID3v2.4.0 tag."""

    album: str = ""
    title: str = ""
    artists: tuple[str, ...] = ()
    track_number: int = 1
    total_tracks: int = 1
    disc_number: int = 1
    total_discs: int = 1
    narrator: str = ""
    cover_bytes: bytes | None = None
    cover_mime: str = ""

    def __post_init__(self) -> None:
        _validate_positive_int("track_number", self.track_number)
        _validate_positive_int("total_tracks", self.total_tracks)
        _validate_positive_int("disc_number", self.disc_number)
        _validate_positive_int("total_discs", self.total_discs)


def build_id3v24_tag(metadata: Id3Metadata) -> bytes:
    """Build a complete ID3v2.4.0 binary tag block for ``metadata``."""
    frames: list[bytes] = []

    album = metadata.album.strip()
    if album:
        frames.append(_build_text_frame(b"TALB", album))

    title = metadata.title.strip()
    if title:
        frames.append(_build_text_frame(b"TIT2", title))

    artists = "; ".join(cleaned for item in metadata.artists if (cleaned := item.strip()))
    if artists:
        frames.append(_build_text_frame(b"TPE1", artists))
        frames.append(_build_text_frame(b"TPE2", artists))

    frames.append(_build_text_frame(b"TRCK", f"{metadata.track_number}/{metadata.total_tracks}"))
    frames.append(_build_text_frame(b"TPOS", f"{metadata.disc_number}/{metadata.total_discs}"))

    narrator = metadata.narrator.strip()
    if narrator:
        frames.append(_build_txxx_frame("narrator", narrator))
        frames.append(_build_text_frame(b"TCOM", narrator))

    apic = _build_apic_frame(metadata.cover_bytes, metadata.cover_mime)
    if apic:
        frames.append(apic)

    body = b"".join(frames)
    return _ID3V24_HEADER_PREFIX + encode_synchsafe(len(body)) + body
