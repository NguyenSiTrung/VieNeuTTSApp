"""Unit tests for the pure-Python ID3v2.4.0 tag builder."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from vienetts_app.core.id3 import (
    EMBED_ALL_TRACKS_COVER_LIMIT,
    MAX_COVER_BYTES,
    MAX_TEXT_FRAME_BYTES,
    Id3Metadata,
    build_id3v24_tag,
    encode_synchsafe,
    mime_for_image_ext,
    should_embed_cover,
    sniff_image_mime,
)


@dataclass(frozen=True)
class _ParsedApic:
    mime: str
    picture_type: int
    description: str
    data: bytes


@dataclass(frozen=True)
class _ParsedFrame:
    frame_id: str
    size: int
    raw_size_bytes: bytes
    flags: bytes
    payload: bytes


@dataclass(frozen=True)
class _ParsedId3:
    header_size: int
    raw_header_size_bytes: bytes
    frames: list[_ParsedFrame]
    text_frames: dict[str, str]
    txxx_frames: dict[str, str]
    apic_frames: list[_ParsedApic]


def _decode_synchsafe(raw: bytes) -> int:
    assert len(raw) == 4, f"expected 4 synchsafe bytes, got {len(raw)}"
    for idx, byte in enumerate(raw):
        assert (byte & 0x80) == 0, f"synchsafe byte {idx} has MSB set: 0x{byte:02x}"
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def _parse_id3v24(tag: bytes) -> _ParsedId3:
    assert len(tag) >= 10, f"tag shorter than 10-byte header: {len(tag)}"
    assert tag[:6] == b"ID3\x04\x00\x00", f"unexpected ID3v2.4.0 header prefix: {tag[:6]!r}"
    raw_header_size = tag[6:10]
    header_size = _decode_synchsafe(raw_header_size)
    assert header_size == len(tag) - 10, (
        f"header synchsafe size {header_size} != body length {len(tag) - 10}"
    )

    frames: list[_ParsedFrame] = []
    text_frames: dict[str, str] = {}
    txxx_frames: dict[str, str] = {}
    apic_frames: list[_ParsedApic] = []

    offset = 10
    while offset < len(tag):
        assert offset + 10 <= len(tag), f"truncated frame header at offset {offset}"
        frame_id = tag[offset : offset + 4].decode("ascii")
        raw_frame_size = tag[offset + 4 : offset + 8]
        frame_size = _decode_synchsafe(raw_frame_size)
        flags = tag[offset + 8 : offset + 10]
        assert flags == b"\x00\x00", f"unexpected frame flags on {frame_id}: {flags!r}"
        offset += 10
        assert offset + frame_size <= len(tag), (
            f"frame {frame_id} payload ({frame_size}) overruns tag end"
        )
        payload = tag[offset : offset + frame_size]
        offset += frame_size

        frames.append(
            _ParsedFrame(
                frame_id=frame_id,
                size=frame_size,
                raw_size_bytes=raw_frame_size,
                flags=flags,
                payload=payload,
            )
        )

        if frame_id == "TXXX":
            assert payload[:1] == b"\x03", "TXXX must use UTF-8 encoding (0x03)"
            nul_idx = payload.index(b"\x00", 1)
            desc = payload[1:nul_idx].decode("utf-8")
            value = payload[nul_idx + 1 :].decode("utf-8")
            txxx_frames[desc] = value
        elif frame_id.startswith("T"):
            assert payload[:1] == b"\x03", f"{frame_id} must use UTF-8 encoding (0x03)"
            text_frames[frame_id] = payload[1:].decode("utf-8")
        elif frame_id == "APIC":
            assert payload[:1] == b"\x03", "APIC must use UTF-8 encoding (0x03)"
            mime_end = payload.index(b"\x00", 1)
            mime = payload[1:mime_end].decode("ascii")
            pic_type = payload[mime_end + 1]
            desc_end = payload.index(b"\x00", mime_end + 2)
            desc = payload[mime_end + 2 : desc_end].decode("utf-8")
            data = payload[desc_end + 1 :]
            apic_frames.append(
                _ParsedApic(
                    mime=mime,
                    picture_type=pic_type,
                    description=desc,
                    data=data,
                )
            )
        else:
            pytest.fail(f"Unexpected frame id in test parser: {frame_id}")

    assert offset == len(tag)
    return _ParsedId3(
        header_size=header_size,
        raw_header_size_bytes=raw_header_size,
        frames=frames,
        text_frames=text_frames,
        txxx_frames=txxx_frames,
        apic_frames=apic_frames,
    )


def _jpeg_bytes(size: int = 32) -> bytes:
    assert size >= 3
    return b"\xff\xd8\xff" + b"\xab" * (size - 3)


def _png_bytes(size: int = 32) -> bytes:
    assert size >= 8
    return b"\x89PNG\r\n\x1a\n" + b"\xcd" * (size - 8)


def _gif_bytes(size: int = 32) -> bytes:
    assert size >= 6
    return b"GIF89a" + b"\xef" * (size - 6)


def _webp_bytes(size: int = 32) -> bytes:
    assert size >= 12
    return b"RIFF\x24\x00\x00\x00WEBP" + b"\x12" * (size - 12)


def test_constants_match_contract() -> None:
    assert MAX_TEXT_FRAME_BYTES == 64 * 1024
    assert MAX_COVER_BYTES == 32 * 1024 * 1024
    assert EMBED_ALL_TRACKS_COVER_LIMIT == 512 * 1024


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00\x00\x00\x00"),
        (1, b"\x00\x00\x00\x01"),
        (127, b"\x00\x00\x00\x7f"),
        (128, b"\x00\x00\x01\x00"),
        (255, b"\x00\x00\x01\x7f"),
        (256, b"\x00\x00\x02\x00"),
        (16_383, b"\x00\x00\x7f\x7f"),
        (16_384, b"\x00\x01\x00\x00"),
        (20_480, b"\x00\x01\x20\x00"),
        (2_097_151, b"\x00\x7f\x7f\x7f"),
        (2_097_152, b"\x01\x00\x00\x00"),
        ((1 << 28) - 1, b"\x7f\x7f\x7f\x7f"),
    ],
)
def test_encode_synchsafe_round_trips(value: int, expected: bytes) -> None:
    encoded = encode_synchsafe(value)
    assert encoded == expected
    assert _decode_synchsafe(encoded) == value


@pytest.mark.parametrize("bad_value", [-1, -100, 1 << 28, (1 << 28) + 5, True, False])
def test_encode_synchsafe_rejects_out_of_range(bad_value: int) -> None:
    with pytest.raises(ValueError):
        encode_synchsafe(bad_value)


@pytest.mark.parametrize(
    ("ext", "expected"),
    [
        ("jpg", "image/jpeg"),
        ("jpeg", "image/jpeg"),
        (".JPG", "image/jpeg"),
        (" .JPEG ", "image/jpeg"),
        ("png", "image/png"),
        (".PnG", "image/png"),
        ("gif", "image/gif"),
        (".GIF", "image/gif"),
        ("webp", "image/webp"),
        (".WebP", "image/webp"),
        ("", ""),
        ("   ", ""),
        ("bmp", ""),
        ("svg", ""),
        ("..jpg", ""),
    ],
)
def test_mime_for_image_ext(ext: str, expected: str) -> None:
    assert mime_for_image_ext(ext) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_jpeg_bytes(3), "image/jpeg"),
        (_jpeg_bytes(64), "image/jpeg"),
        (_png_bytes(8), "image/png"),
        (_png_bytes(64), "image/png"),
        (b"GIF89a", "image/gif"),
        (b"GIF87a", "image/gif"),
        (b"GIF8\x00\x00", "image/gif"),
        (b"GIF89", ""),
        (_webp_bytes(12), "image/webp"),
        (_webp_bytes(64), "image/webp"),
        (b"RIFF\x00\x00\x00\x00WEB", ""),
        (b"RIFF\x00\x00\x00\x00WAVE1234", ""),
        (b"", ""),
        (b"\xff\xd8", ""),
        (b"\x89PNG\r\n\x1a", ""),
        (b"not an image", ""),
    ],
)
def test_sniff_image_mime(data: bytes, expected: str) -> None:
    assert sniff_image_mime(data) == expected


def test_should_embed_cover_thresholds_and_validation() -> None:
    small = _jpeg_bytes(128)
    at_limit = _jpeg_bytes(EMBED_ALL_TRACKS_COVER_LIMIT)
    over_limit = _jpeg_bytes(EMBED_ALL_TRACKS_COVER_LIMIT + 1)
    at_max = _jpeg_bytes(MAX_COVER_BYTES)
    over_max = _jpeg_bytes(MAX_COVER_BYTES + 1)

    assert should_embed_cover(None, 1) is False
    assert should_embed_cover(b"", 1) is False
    assert should_embed_cover(b"corrupt cover", 1) is False
    assert should_embed_cover(over_max, 1) is False

    # Small and exact 512 KiB covers embed on every track.
    assert should_embed_cover(small, 1) is True
    assert should_embed_cover(small, 2) is True
    assert should_embed_cover(at_limit, 1) is True
    assert should_embed_cover(at_limit, 2) is True
    assert should_embed_cover(at_limit, 99) is True

    # > 512 KiB (up to 32 MiB) embeds on track 1 only.
    assert should_embed_cover(over_limit, 1) is True
    assert should_embed_cover(over_limit, 2) is False
    assert should_embed_cover(at_max, 1) is True
    assert should_embed_cover(at_max, 2) is False


def test_build_id3v24_tag_vietnamese_full_round_trip() -> None:
    cover = _jpeg_bytes(64)
    meta = Id3Metadata(
        album="Đất Rừng Phương Nam",
        title="Chương 1: Hành trình về phương Nam",
        artists=("  Đoàn Giỏi  ", "", "   ", "Nguyễn Quang Sáng"),
        track_number=3,
        total_tracks=12,
        disc_number=1,
        total_discs=2,
        narrator="Ngọc Anh",
        cover_bytes=cover,
    )

    tag = build_id3v24_tag(meta)
    parsed = _parse_id3v24(tag)

    assert parsed.text_frames == {
        "TALB": "Đất Rừng Phương Nam",
        "TIT2": "Chương 1: Hành trình về phương Nam",
        "TPE1": "Đoàn Giỏi; Nguyễn Quang Sáng",
        "TPE2": "Đoàn Giỏi; Nguyễn Quang Sáng",
        "TRCK": "3/12",
        "TPOS": "1/2",
        "TCOM": "Ngọc Anh",
    }
    assert parsed.txxx_frames == {"narrator": "Ngọc Anh"}
    assert len(parsed.apic_frames) == 1
    assert parsed.apic_frames[0] == _ParsedApic(
        mime="image/jpeg",
        picture_type=0x03,
        description="",
        data=cover,
    )


def test_build_id3v24_tag_synchsafe_sizes_above_127_and_16383() -> None:
    long_title = "Chương mở đầu — " + " ".join(["Tiếng gọi nơi hoang dã."] * 10)
    assert len(long_title.encode("utf-8")) > 127
    large_cover = _png_bytes(20 * 1024)
    assert len(large_cover) > 16_383

    tag = build_id3v24_tag(
        Id3Metadata(
            album="Tuyển tập truyện ngắn",
            title=long_title,
            track_number=1,
            total_tracks=5,
            cover_bytes=large_cover,
        )
    )
    parsed = _parse_id3v24(tag)

    # Overall tag size > 16,383 exercises 3-byte synchsafe encoding.
    assert parsed.header_size > 16_383
    assert parsed.raw_header_size_bytes[1] > 0

    tit2 = next(f for f in parsed.frames if f.frame_id == "TIT2")
    assert tit2.size > 127
    assert tit2.raw_size_bytes[2] > 0

    apic = next(f for f in parsed.frames if f.frame_id == "APIC")
    assert apic.size > 16_383
    assert apic.raw_size_bytes[1] > 0
    assert parsed.text_frames["TIT2"] == long_title
    assert parsed.apic_frames[0].mime == "image/png"
    assert parsed.apic_frames[0].data == large_cover


def test_build_id3v24_tag_omits_blank_optional_frames_but_keeps_trck_and_tpos() -> None:
    tag = build_id3v24_tag(
        Id3Metadata(
            album="   ",
            title="\t\n",
            artists=("", "   "),
            narrator="  ",
        )
    )
    parsed = _parse_id3v24(tag)

    assert [f.frame_id for f in parsed.frames] == ["TRCK", "TPOS"]
    assert parsed.text_frames == {"TRCK": "1/1", "TPOS": "1/1"}
    assert parsed.txxx_frames == {}
    assert parsed.apic_frames == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"track_number": 0},
        {"track_number": -1},
        {"total_tracks": 0},
        {"total_tracks": -2},
        {"disc_number": 0},
        {"disc_number": -1},
        {"total_discs": 0},
        {"total_discs": -3},
        {"track_number": False},
        {"total_tracks": True},
    ],
)
def test_id3_metadata_rejects_non_positive_track_or_disc_numbers(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Id3Metadata(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cover_bytes", "expected_mime"),
    [
        (_jpeg_bytes(48), "image/jpeg"),
        (_png_bytes(48), "image/png"),
        (_gif_bytes(48), "image/gif"),
        (_webp_bytes(48), "image/webp"),
    ],
)
def test_build_id3v24_tag_apic_supports_all_four_image_formats(
    cover_bytes: bytes, expected_mime: str
) -> None:
    tag = build_id3v24_tag(Id3Metadata(cover_bytes=cover_bytes))
    parsed = _parse_id3v24(tag)

    assert len(parsed.apic_frames) == 1
    apic = parsed.apic_frames[0]
    assert apic.mime == expected_mime
    assert apic.picture_type == 0x03
    assert apic.description == ""
    assert apic.data == cover_bytes


def test_build_id3v24_tag_apic_explicit_mime_and_omission_rules() -> None:
    # Explicit MIME normalized to lowercase ASCII.
    raw = _png_bytes(32)
    tag = build_id3v24_tag(Id3Metadata(cover_bytes=raw, cover_mime=" IMAGE/PNG "))
    parsed = _parse_id3v24(tag)
    assert len(parsed.apic_frames) == 1
    assert parsed.apic_frames[0].mime == "image/png"

    # Unrecognized bytes with empty MIME -> omitted.
    parsed_unknown = _parse_id3v24(build_id3v24_tag(Id3Metadata(cover_bytes=b"not an image")))
    assert parsed_unknown.apic_frames == []

    # Unsupported explicit MIME -> omitted.
    parsed_bmp = _parse_id3v24(
        build_id3v24_tag(Id3Metadata(cover_bytes=_jpeg_bytes(32), cover_mime="image/bmp"))
    )
    assert parsed_bmp.apic_frames == []

    # Empty cover -> omitted.
    parsed_empty = _parse_id3v24(build_id3v24_tag(Id3Metadata(cover_bytes=b"")))
    assert parsed_empty.apic_frames == []

    # Oversized (> 32 MiB) cover -> omitted.
    oversized = _jpeg_bytes(MAX_COVER_BYTES + 1)
    parsed_oversized = _parse_id3v24(build_id3v24_tag(Id3Metadata(cover_bytes=oversized)))
    assert parsed_oversized.apic_frames == []


@pytest.mark.parametrize(
    ("char", "char_bytes"),
    [
        ("é", 2),
        ("ấ", 3),
        ("🎧", 4),
    ],
)
def test_build_id3v24_tag_truncates_text_and_txxx_on_utf8_boundary(
    char: str, char_bytes: int
) -> None:
    # Pad with ASCII 'a' so that the multi-byte character straddles the exact 64 KiB boundary
    # for both standard text frames (1-byte prefix) and TXXX:narrator (10-byte prefix).
    text_prefix_len = MAX_TEXT_FRAME_BYTES - 1 - (char_bytes - 1)
    long_title = ("a" * text_prefix_len) + char + "tail"
    assert len(long_title.encode("utf-8")) > MAX_TEXT_FRAME_BYTES

    txxx_prefix_len = MAX_TEXT_FRAME_BYTES - len(b"\x03narrator\x00") - (char_bytes - 1)
    long_narrator = ("b" * txxx_prefix_len) + char + "tail"

    tag = build_id3v24_tag(
        Id3Metadata(
            album=long_title,
            title=long_title,
            narrator=long_narrator,
        )
    )
    parsed = _parse_id3v24(tag)

    for frame in parsed.frames:
        assert frame.size <= MAX_TEXT_FRAME_BYTES

    assert parsed.text_frames["TIT2"] == "a" * text_prefix_len
    assert parsed.text_frames["TALB"] == "a" * text_prefix_len
    assert parsed.txxx_frames["narrator"] == "b" * txxx_prefix_len
    assert long_narrator.startswith(parsed.text_frames["TCOM"])


def test_build_id3v24_tag_truncates_long_vietnamese_paragraph_cleanly() -> None:
    phrase = "Đất Rừng Phương Nam — tiếng chim hót giữa rừng U Minh hạ. "
    long_text = phrase * 1_500
    assert len(long_text.encode("utf-8")) > MAX_TEXT_FRAME_BYTES

    tag = build_id3v24_tag(
        Id3Metadata(
            album=long_text,
            title=long_text,
            artists=(long_text,),
            narrator=long_text,
        )
    )
    parsed = _parse_id3v24(tag)

    for frame in parsed.frames:
        assert frame.size <= MAX_TEXT_FRAME_BYTES

    for frame_id in ("TALB", "TIT2", "TPE1", "TPE2", "TCOM"):
        decoded = parsed.text_frames[frame_id]
        assert long_text.startswith(decoded)
        assert len(decoded.encode("utf-8")) <= MAX_TEXT_FRAME_BYTES - 1
        assert len(decoded.encode("utf-8")) >= MAX_TEXT_FRAME_BYTES - 4

    decoded_txxx = parsed.txxx_frames["narrator"]
    assert long_text.startswith(decoded_txxx)
    assert len(decoded_txxx.encode("utf-8")) <= MAX_TEXT_FRAME_BYTES - 10
    assert len(decoded_txxx.encode("utf-8")) >= MAX_TEXT_FRAME_BYTES - 13
