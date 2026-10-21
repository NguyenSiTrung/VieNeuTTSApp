"""Unit tests for core/asr.py: segment normalization, local Whisper adapters, and API endpoint."""

from __future__ import annotations

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core.asr import (
    AsrCancelled,
    AsrConfig,
    AsrError,
    RawSegment,
    normalize_asr_segments_to_cues,
    transcribe_media_to_cues,
)
from vienetts_app.core.audio import write_wav_file


def test_normalize_asr_segments_splits_long_and_merges_micro() -> None:
    segments = [
        # Micro fragment (<400ms) followed closely (<150ms gap) by the rest of the phrase
        RawSegment(start_ms=100, end_ms=350, text="Xin", speaker="SPEAKER_01"),
        RawSegment(start_ms=420, end_ms=1800, text="chào các bạn.", speaker="SPEAKER_01"),
        # Overlong segment (>84 chars) with clause punctuation that should split proportionally
        RawSegment(
            start_ms=2000,
            end_ms=10_000,
            text=(
                "Hôm nay chúng ta sẽ cùng tìm hiểu về quy trình lồng tiếng tự động cho video, "
                "từ bước nhận diện giọng nói đến bước ghép âm thanh vào tệp gốc."
            ),
            speaker="SPEAKER_02",
        ),
    ]
    cues = normalize_asr_segments_to_cues(segments, max_chars=84, max_duration_ms=7000)
    assert len(cues) == 3
    assert cues[0].index == 1
    assert cues[0].start_ms == 100
    assert cues[0].end_ms == 1800
    assert cues[0].text == "Xin chào các bạn."
    assert cues[0].speaker == "SPEAKER_01"

    assert cues[1].speaker == "SPEAKER_02"
    assert cues[2].speaker == "SPEAKER_02"
    assert cues[1].start_ms == 2000
    assert cues[1].end_ms == cues[2].start_ms
    assert cues[2].end_ms == 10_000
    assert all(len(c.text) <= 84 for c in cues)


def test_non_speech_filtering_duration_split_and_backward_micro_merge() -> None:
    segments = [
        RawSegment(0, 1000, "[BLANK_AUDIO]"),
        RawSegment(1000, 2000, "[MUSIC]"),
        RawSegment(2000, 3000, "(applause)"),
        RawSegment(3000, 4000, "♪ ♪"),
        RawSegment(4000, 5500, "[MUSIC] Xin chào mọi người.", speaker="MUSIC"),
        # Trailing micro-fragment (<400 ms, gap 80 ms, same speaker) that must merge backward
        RawSegment(5580, 5850, "nhé!", speaker=""),
        # Short text (49 chars <= 84) but overlong duration (12_000 ms > 7_000 ms) with 2 clauses
        RawSegment(
            6000,
            18_000,
            "Mệnh đề thứ nhất ở đây, mệnh đề thứ hai tiếp nối.",
            speaker="SPEAKER_01",
        ),
    ]
    cues = normalize_asr_segments_to_cues(segments, max_chars=84, max_duration_ms=7000)
    assert len(cues) == 3
    assert cues[0].text == "Xin chào mọi người. nhé!"
    assert cues[0].speaker == ""
    assert cues[0].start_ms == 4000 and cues[0].end_ms == 5850
    assert cues[1].text == "Mệnh đề thứ nhất ở đây,"
    assert cues[2].text == "mệnh đề thứ hai tiếp nối."
    assert cues[1].duration_ms <= 7000
    assert cues[2].duration_ms <= 7000


def test_transcribe_with_faster_whisper_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = write_wav_file(np.zeros(16_000, dtype=np.float32), tmp_path / "speech.wav", 16_000)

    class _FakeSeg:
        def __init__(self, start: float, end: float, text: str) -> None:
            self.start = start
            self.end = end
            self.text = text

    class _FakeInfo:
        duration = 4.0
        language = "vi"

    class _FakeWhisperModel:
        def __init__(self, model_size: str, **kwargs: object) -> None:
            self.model_size = model_size

        def transcribe(self, path: str, **kwargs: object):
            return (
                iter(
                    [
                        _FakeSeg(0.0, 1.8, " Câu thứ nhất. "),
                        _FakeSeg(2.0, 3.8, " Câu thứ hai. "),
                    ]
                ),
                _FakeInfo(),
            )

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    progress_steps: list[float] = []
    cues = transcribe_media_to_cues(
        wav,
        AsrConfig(backend="faster_whisper", model="base", language="vi"),
        progress_cb=lambda p, _msg: progress_steps.append(p),
    )
    assert [c.text for c in cues] == ["Câu thứ nhất.", "Câu thứ hai."]
    assert cues[0].start_ms == 0 and cues[0].end_ms == 1800
    assert progress_steps and progress_steps[-1] == pytest.approx(1.0)


def test_transcribe_with_openai_compatible_api_endpoint(tmp_path: Path) -> None:
    wav = write_wav_file(np.zeros(8_000, dtype=np.float32), tmp_path / "clip.wav", 16_000)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            assert b"clip.wav" in body
            payload = {
                "segments": [
                    {"start": 0.1, "end": 1.4, "text": "Xin chào từ máy chủ ASR."},
                    {"start": 1.6, "end": 2.9, "text": "Phụ đề thứ hai."},
                ]
            }
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cues = transcribe_media_to_cues(
            wav,
            AsrConfig(
                backend="openai_api",
                model="whisper-1",
                language="vi",
                api_base_url=f"http://127.0.0.1:{port}/v1",
            ),
        )
        assert len(cues) == 2
        assert cues[0].text == "Xin chào từ máy chủ ASR."
        assert cues[0].start_ms == 100 and cues[0].end_ms == 1400
    finally:
        server.shutdown()
        server.server_close()


def test_transcribe_BuiltIn_acoustic_vad_fallback_when_no_external_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ensure neither faster_whisper nor whisper is imported so auto falls back
    # cleanly or raises a clear AsrError when no backend is available.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "whisper", None)
    wav = write_wav_file(np.zeros(8_000, dtype=np.float32), tmp_path / "silent.wav", 16_000)
    with pytest.raises(AsrError):
        transcribe_media_to_cues(
            wav,
            AsrConfig(backend="faster_whisper", model="base"),
        )


def test_wav_48k_stereo_normalized_sidecar_invalidated_and_segments_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import soundfile as sf

    # 1. Create a 48 kHz stereo WAV and verify whisper-cli receives a 16 kHz mono WAV
    stereo_48k = np.column_stack(
        [
            np.linspace(-0.2, 0.2, 48_000, dtype=np.float32),
            np.linspace(0.2, -0.2, 48_000, dtype=np.float32),
        ]
    )
    src_wav = tmp_path / "stereo_48k.wav"
    sf.write(str(src_wav), stereo_48k, 48_000, subtype="PCM_16")

    bin_dir = tmp_path / "runtimes" / "whisper" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_name = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    cli_path = bin_dir / cli_name
    seen_wav_log = tmp_path / "seen_wav.txt"
    cli_path.write_text(
        "#!/bin/sh\n"
        'wav=""\n'
        'of=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -f) shift; wav="$1" ;;\n'
        '    -of) shift; of="$1" ;;\n'
        "  esac\n"
        "  shift\n"
        "done\n"
        f'echo "$wav" >> "{seen_wav_log}"\n'
        'printf "1\\n00:00:00,000 --> 00:00:01,000\\n"'
        '"Xin chào từ whisper-cli.\\n\\n" > "${of}.srt"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    cli_path.chmod(0o755)

    from vienetts_app.core.ai_models import WHISPER_GGUF_MODELS

    model_dir = tmp_path / "models" / "whisper"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_file = model_dir / WHISPER_GGUF_MODELS["base"].filename
    with model_file.open("wb") as fh:
        fh.truncate(int(WHISPER_GGUF_MODELS["base"].size_bytes))

    work_dir = tmp_path / "work"
    cfg = AsrConfig(backend="whisper_cpp", model="base", language="vi")
    cues1 = transcribe_media_to_cues(src_wav, cfg, work_dir=work_dir, data_dir=tmp_path)
    assert [c.text for c in cues1] == ["Xin chào từ whisper-cli."]

    invocations = seen_wav_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(invocations) == 1
    passed_wav = Path(invocations[0])
    assert passed_wav != src_wav
    info = sf.info(str(passed_wav))
    assert info.samplerate == 16_000
    assert info.channels == 1
    sidecar = work_dir / "input_16k.wav.json"
    assert sidecar.is_file()
    assert (work_dir / "asr_segments_cache.json").is_file()

    # 2. Second call with unchanged input serves from asr_segments_cache.json without invoking CLI
    cues2 = transcribe_media_to_cues(src_wav, cfg, work_dir=work_dir, data_dir=tmp_path)
    assert cues2 == cues1
    assert len(seen_wav_log.read_text(encoding="utf-8").strip().splitlines()) == 1

    # 3. Modifying the source WAV invalidates both input_16k.wav sidecar and segment cache
    stereo_48k_2 = np.zeros((96_000, 2), dtype=np.float32)
    sf.write(str(src_wav), stereo_48k_2, 48_000, subtype="PCM_16")
    cues3 = transcribe_media_to_cues(src_wav, cfg, work_dir=work_dir, data_dir=tmp_path)
    assert [c.text for c in cues3] == ["Xin chào từ whisper-cli."]
    assert len(seen_wav_log.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert sf.info(str(work_dir / "input_16k.wav")).frames == 32_000


def test_auto_cancel_is_not_swallowed_and_fallback_notes_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = write_wav_file(np.zeros(16_000, dtype=np.float32), tmp_path / "cancel.wav", 16_000)
    calls: list[str] = []

    class _CancelWhisperModel:
        def __init__(self, model_size: str, **kwargs: object) -> None:
            calls.append("faster_whisper")

        def transcribe(self, path: str, **kwargs: object):
            class _Seg:
                start = 0.0
                end = 1.0
                text = "Xin chào"

            class _Info:
                duration = 1.0

            return iter([_Seg()]), _Info()

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _CancelWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    # 1. Cancelling mid-stream raises AsrCancelled and never invokes whisper_cpp or openai_api
    with pytest.raises(AsrCancelled):
        transcribe_media_to_cues(
            wav,
            AsrConfig(backend="auto", api_base_url="http://127.0.0.1:9/v1"),
            cancel_check=lambda: True,
        )

    # 2. When whisper.cpp is installed locally, auto prefers it first; if it fails at runtime,
    # auto falls back to faster_whisper and records the reason in notes.
    import os

    from vienetts_app.core.ai_models import WHISPER_GGUF_MODELS

    bin_dir = tmp_path / "runtimes" / "whisper" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_name = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    cli_path = bin_dir / cli_name
    cli_path.write_text("#!/bin/sh\necho 'GPU init error' >&2\nexit 2\n", encoding="utf-8")
    cli_path.chmod(0o755)
    model_dir = tmp_path / "models" / "whisper"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_file = model_dir / WHISPER_GGUF_MODELS["base"].filename
    with model_file.open("wb") as fh:
        fh.truncate(int(WHISPER_GGUF_MODELS["base"].size_bytes))

    notes: list[str] = []
    cues = transcribe_media_to_cues(
        wav,
        AsrConfig(backend="auto", model="base"),
        data_dir=tmp_path,
        notes=notes,
    )
    assert [c.text for c in cues] == ["Xin chào"]
    assert any("GPU init error" in n for n in notes)

    # 3. If faster_whisper is also absent and only the Default Ollama URL is passed,
    # auto surfaces the real whisper.cpp error instead of masking it with a generic error.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(AsrError, match="GPU init error"):
        transcribe_media_to_cues(
            wav,
            AsrConfig(backend="auto", model="base", api_base_url="http://localhost:11434/v1"),
            data_dir=tmp_path,
        )


def test_whisper_cpp_flags_progress_utf8_and_timeout(tmp_path: Path) -> None:
    import os

    from vienetts_app.core.ai_models import WHISPER_GGUF_MODELS

    wav = write_wav_file(np.zeros(16_000 * 2, dtype=np.float32), tmp_path / "speech.wav", 16_000)
    bin_dir = tmp_path / "runtimes" / "whisper" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_name = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    cli_path = bin_dir / cli_name
    args_log = tmp_path / "args.txt"
    cli_path.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{args_log}"\n'
        'of=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-of" ]; then shift; of="$1"; fi\n'
        "  shift\n"
        "done\n"
        'echo "whisper_print_progress_callback: progress =  50%" >&2\n'
        'printf "1\\n00:00:00,000 --> 00:00:01,500\\nTiếng Việt có dấu.\\n\\n" > "${of}.srt"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    cli_path.chmod(0o755)

    model_dir = tmp_path / "models" / "whisper"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_file = model_dir / WHISPER_GGUF_MODELS["base"].filename
    with model_file.open("wb") as fh:
        fh.truncate(int(WHISPER_GGUF_MODELS["base"].size_bytes))

    progress: list[float] = []
    cues = transcribe_media_to_cues(
        wav,
        AsrConfig(backend="whisper_cpp", model="base", device="cpu", threads=6),
        data_dir=tmp_path,
        progress_cb=lambda p, _m: progress.append(p),
    )
    assert [c.text for c in cues] == ["Tiếng Việt có dấu."]
    logged_args = args_log.read_text(encoding="utf-8")
    assert "-t 6" in logged_args
    assert "-sns" in logged_args
    assert "-pp" in logged_args
    assert "-ng" in logged_args
    assert any(0.5 < p < 0.96 for p in progress)

    # Timeout kills hung process
    cli_path.write_text("#!/bin/sh\nsleep 5\nexit 0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    with pytest.raises(AsrError, match="quá thời gian chờ"):
        transcribe_media_to_cues(
            wav,
            AsrConfig(backend="whisper_cpp", model="base", timeout_s=0.3),
            data_dir=tmp_path,
        )


def test_openai_api_json_with_arrow_and_top_level_text_duration(tmp_path: Path) -> None:
    # 3-second WAV (48_000 frames @ 16 kHz)
    wav = write_wav_file(np.zeros(48_000, dtype=np.float32), tmp_path / "arrow.wav", 16_000)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(length)
            # JSON response containing "-->" inside text, top-level "text" only (no segments)
            payload = {"text": "Bước A --> Bước B trong bài giảng."}
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cues = transcribe_media_to_cues(
            wav,
            AsrConfig(
                backend="openai_api",
                model="whisper-1",
                api_base_url=f"http://127.0.0.1:{server.server_port}/v1",
            ),
        )
        assert len(cues) == 1
        assert cues[0].text == "Bước A --> Bước B trong bài giảng."
        assert cues[0].start_ms == 0
        assert cues[0].end_ms == 3000
    finally:
        server.shutdown()
        server.server_close()
