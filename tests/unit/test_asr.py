"""Unit tests for core/asr.py: segment normalization, local Whisper adapters, and API endpoint."""

from __future__ import annotations

import io
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core import asr
from vienetts_app.core.asr import (
    AsrCancelled,
    AsrConfig,
    AsrError,
    RawSegment,
    _asr_cache_key,
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
    bodies: list[bytes] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            bodies.append(body)
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
                model="base",
                language="vi",
                api_base_url=f"http://127.0.0.1:{port}/v1",
            ),
        )
        assert len(cues) == 2
        assert cues[0].text == "Xin chào từ máy chủ ASR."
        assert cues[0].start_ms == 100 and cues[0].end_ms == 1400
        assert b'name="model"\r\n\r\nwhisper-1\r\n' in bodies[-1]
        transcribe_media_to_cues(
            wav,
            AsrConfig(
                backend="openai_api",
                model="base",
                api_model="whisper-large-v3-turbo",
                api_base_url=f"http://127.0.0.1:{port}/v1",
            ),
        )
        assert b'name="model"\r\n\r\nwhisper-large-v3-turbo\r\n' in bodies[-1]
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
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "wav = args[args.index('-f') + 1]\n"
        "of = args[args.index('-of') + 1]\n"
        f"with open({str(seen_wav_log.as_posix())!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(wav + '\\n')\n"
        "Path(of + '.srt').write_text(\n"
        "    '1\\n00:00:00,000 --> 00:00:01,000\\nXin chào từ whisper-cli.\\n\\n',\n"
        "    encoding='utf-8',\n"
        ")\n",
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
    shared = work_dir / "source_16k.wav"
    assert passed_wav == shared
    sidecar = work_dir / "source_16k.wav.json"
    assert sidecar.is_file()
    segments_cache = work_dir / "asr_segments_cache.json"
    assert segments_cache.is_file()

    # 2. Second call with unchanged input serves from asr_segments_cache.json without invoking CLI
    cues2 = transcribe_media_to_cues(src_wav, cfg, work_dir=work_dir, data_dir=tmp_path)
    assert cues2 == cues1
    assert len(seen_wav_log.read_text(encoding="utf-8").strip().splitlines()) == 1

    # 2b. A pre-upgrade cache dir (ASR's own input_16k.wav, its unversioned sidecar and
    # segments keyed on that path) is adopted without reconversion and still hits the cache.
    stamp = shared.stat().st_mtime_ns
    legacy_sidecar = json.loads(sidecar.read_text(encoding="utf-8"))
    legacy_sidecar.pop("format")
    (work_dir / "input_16k.wav.json").write_text(json.dumps(legacy_sidecar), encoding="utf-8")
    sidecar.unlink()
    shared.rename(work_dir / "input_16k.wav")
    segments_cache.write_text(
        segments_cache.read_text(encoding="utf-8").replace("source_16k.wav", "input_16k.wav"),
        encoding="utf-8",
    )
    assert transcribe_media_to_cues(src_wav, cfg, work_dir=work_dir, data_dir=tmp_path) == cues1
    assert len(seen_wav_log.read_text(encoding="utf-8").strip().splitlines()) == 1
    assert shared.stat().st_mtime_ns == stamp
    assert not (work_dir / "input_16k.wav").exists()

    # 2c. A segments cache from another ASR version (or none, pre-versioning) re-runs cleanly.
    stale = json.loads(segments_cache.read_text(encoding="utf-8"))
    assert stale.pop("asr_version") == asr.ASR_VERSION
    segments_cache.write_text(json.dumps(stale), encoding="utf-8")
    assert transcribe_media_to_cues(src_wav, cfg, work_dir=work_dir, data_dir=tmp_path) == cues1
    assert len(seen_wav_log.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert json.loads(segments_cache.read_text(encoding="utf-8"))["asr_version"] == asr.ASR_VERSION

    # 3. Modifying the source WAV invalidates both the shared WAV sidecar and segment cache
    stereo_48k_2 = np.zeros((96_000, 2), dtype=np.float32)
    sf.write(str(src_wav), stereo_48k_2, 48_000, subtype="PCM_16")
    cues3 = transcribe_media_to_cues(src_wav, cfg, work_dir=work_dir, data_dir=tmp_path)
    assert [c.text for c in cues3] == ["Xin chào từ whisper-cli."]
    assert len(seen_wav_log.read_text(encoding="utf-8").strip().splitlines()) == 3
    assert sf.info(str(shared)).frames == 32_000


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
    cli_path.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('GPU init error\\n')\nsys.exit(2)\n",
        encoding="utf-8",
    )
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

    # 3. If faster_whisper is also absent and no ASR URL is passed,
    # auto surfaces the real whisper.cpp error without contacting an API.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    api_calls: list[str] = []

    def fake_api(path, config, **kwargs):
        api_calls.append(config.api_base_url)
        return [RawSegment(0, 1000, "API")]

    monkeypatch.setattr("vienetts_app.core.asr._run_openai_whisper_api", fake_api)
    with pytest.raises(AsrError, match="GPU init error"):
        transcribe_media_to_cues(
            wav,
            AsrConfig(backend="auto", model="base"),
            data_dir=tmp_path,
        )
    assert not api_calls
    cues = transcribe_media_to_cues(
        wav,
        AsrConfig(backend="auto", model="base", api_base_url="http://localhost:11434/v1"),
        data_dir=tmp_path,
    )
    assert [cue.text for cue in cues] == ["API"]
    assert api_calls == ["http://localhost:11434/v1"]


def test_api_error_detail_redacts_key_and_cache_key_is_sparse(tmp_path: Path) -> None:
    wav = write_wav_file(np.zeros(8000, dtype=np.float32), tmp_path / "error.wav", 16_000)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "invalid secret"}}).encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(AsrError) as caught:
            transcribe_media_to_cues(
                wav,
                AsrConfig(
                    backend="openai_api",
                    api_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    api_key="secret",
                ),
            )
        assert "invalid" in str(caught.value) and "secret" not in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()
    base = _asr_cache_key(wav, AsrConfig())
    assert _asr_cache_key(wav, AsrConfig(api_model="different")) == base
    remote = AsrConfig(api_base_url="http://example.invalid/v1", api_model="remote-model")
    assert _asr_cache_key(wav, remote) != base
    assert "remote-model" in _asr_cache_key(wav, remote)


def test_chunked_api_silence_offsets_retry_prompt_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import soundfile as sf

    monkeypatch.setattr(asr, "API_CHUNK_SECONDS", 10)
    monkeypatch.setattr(asr.tempfile, "tempdir", str(tmp_path))
    samples = np.full(25 * 16_000, 0.3, dtype=np.float32)
    samples[int(9.4 * 16_000) : int(9.8 * 16_000)] = 0
    samples[int(18.8 * 16_000) : int(19.4 * 16_000)] = 0
    wav = write_wav_file(samples, tmp_path / "long.wav", 16_000)
    chunks = asr._plan_api_chunks(wav)
    assert len(chunks) == 3
    assert 9.4 <= chunks[0][1] / 16_000 <= 9.8
    assert 18.8 <= chunks[1][1] / 16_000 <= 19.4
    assert chunks[0][1] == chunks[1][0] and chunks[1][1] == chunks[2][0]
    bodies: list[bytes] = []
    status = "retry"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            bodies.append(body)
            if status == "error" or (status == "retry" and len(bodies) == 2):
                self.send_response(401 if status == "error" else 503)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"unavailable"}}')
                return
            payload = {
                "segments": [
                    {
                        "start": 0.1,
                        "end": 0.7,
                        "text": f"chunk{len(bodies)}",
                        "words": [{"word": "hello", "start": 0.2, "end": 0.5}],
                    }
                ]
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = AsrConfig(
        backend="openai_api", api_base_url=f"http://127.0.0.1:{server.server_port}/v1"
    )
    try:
        result = asr._run_openai_whisper_api(wav, config)
        assert len(bodies) == 4 and len(result) == 3
        for index, segment in enumerate(result):
            offset_ms = round(chunks[index][0] * 1000 / 16_000)
            assert segment.start_ms == offset_ms + 100
            assert segment.words[0][:2] == (offset_ms + 200, offset_ms + 500)
        assert b'name="prompt"\r\n\r\nchunk1\r\n' in bodies[1]
        assert b'name="prompt"\r\n\r\nchunk3\r\n' in bodies[3]
        for body in bodies:
            audio = body.split(b"\r\n\r\nRIFF", 1)[1]
            info = sf.info(io.BytesIO(b"RIFF" + audio))
            assert info.subtype == "PCM_16" and info.duration <= 10
            assert len(body) < 25_000_000
        assert 600 * 16_000 * 2 < 25_000_000
        assert not list(tmp_path.glob("vieneu-asr-*"))
        bodies.clear()
        status = "cancel"
        with pytest.raises(AsrCancelled):
            asr._run_openai_whisper_api(wav, config, cancel_check=lambda: len(bodies) >= 1)
        assert len(bodies) == 1 and not list(tmp_path.glob("vieneu-asr-*"))
        bodies.clear()
        status = "error"
        with pytest.raises(AsrError):
            asr._run_openai_whisper_api(wav, config)
        assert len(bodies) == 1 and not list(tmp_path.glob("vieneu-asr-*"))
    finally:
        server.shutdown()
        server.server_close()


def test_short_large_float_wav_is_converted_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import soundfile as sf

    monkeypatch.setattr(asr.tempfile, "tempdir", str(tmp_path))
    wav = write_wav_file(np.zeros(6_250_000, dtype=np.float32), tmp_path / "float.wav", 16_000)
    seen: list[Path] = []

    def fake_upload(path, config, **kwargs):
        seen.append(path)
        assert sf.info(str(path)).subtype == "PCM_16"
        assert path.stat().st_size < 25_000_000
        return [RawSegment(0, 100, "ok")]

    monkeypatch.setattr(asr, "_upload_whisper_api", fake_upload)
    assert asr._run_openai_whisper_api(wav, AsrConfig()) == [RawSegment(0, 100, "ok")]
    assert len(seen) == 1 and not seen[0].exists()
    assert not list(tmp_path.glob("vieneu-asr-*"))


def _whisper_json_full(
    segments: list[tuple[int, int, bytes, list[tuple[bytes, int, int, float]]]],
) -> bytes:
    """``whisper-cli -ojf`` output as raw bytes, laid out like ``cli.cpp``'s writer.

    Token texts are written unescaped, so a pre-b5454 build may split one UTF-8
    character across two tokens (invalid UTF-8 inside each JSON string).
    """
    items = []
    for t0, t1, text, tokens in segments:
        toks = b",\n".join(
            b'\t\t\t\t{\n\t\t\t\t\t"text": "%s",\n'
            b'\t\t\t\t\t"offsets": {\n\t\t\t\t\t\t"from": %d,\n\t\t\t\t\t\t"to": %d\n\t\t\t\t\t},\n'
            b'\t\t\t\t\t"id": 1,\n\t\t\t\t\t"p": %g,\n\t\t\t\t\t"t_dtw": -1\n\t\t\t\t}'
            % (tok, a, b, p)
            for tok, a, b, p in tokens
        )
        items.append(
            b'\t\t{\n\t\t\t"offsets": {\n\t\t\t\t"from": %d,\n\t\t\t\t"to": %d\n\t\t\t},\n'
            b'\t\t\t"text": "%s",\n\t\t\t"tokens": [\n%s\n\t\t\t]\n\t\t}' % (t0, t1, text, toks)
        )
    return (
        b'{\n\t"systeminfo": "NEON = 1",\n\t"result": {\n\t\t"language": "vi"\n\t},\n'
        b'\t"transcription": [\n' + b",\n".join(items) + b"\n\t]\n}\n"
    )


def test_whisper_cpp_flags_json_full_words_cache_dir_progress_and_timeout(
    tmp_path: Path,
) -> None:
    import math
    import os

    from vienetts_app.core.ai_models import (
        AUDIO_MODELS,
        WHISPER_GGUF_MODELS,
        WHISPER_VAD_MODEL_ID,
    )

    # An already-16 kHz mono WAV is transcribed in place, beside the user's other files.
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    wav = write_wav_file(np.zeros(16_000 * 4, dtype=np.float32), user_dir / "speech.wav", 16_000)
    fixture = tmp_path / "fixture.json"
    fixture.write_bytes(
        _whisper_json_full(
            [
                (
                    0,
                    2000,
                    b" Xin ch\xc3\xa0o c\xc3\xa1c b\xe1\xba\xa1n.",
                    [
                        (b"[_BEG_]", 0, 0, 0.99),
                        (b" Xin", 0, 400, 0.9),
                        (b" ch", 400, 600, 0.8),
                        (b"\xc3\xa0o", 600, 900, 0.9),
                        (b" c\xc3\xa1c", 900, 1300, 0.9),
                        (b" b\xe1\xba", 1300, 1500, 0.5),  # "ạ" split across two tokens
                        (b"\xa1n", 1500, 1700, 0.5),
                        (b".", 1700, 1800, 0.9),
                        (b"[_TT_90]", 1800, 1800, 0.9),
                    ],
                ),
                (
                    2000,
                    3500,
                    b" T\xe1\xba\xa1m bi\xe1\xbb\x87t",
                    [
                        (b"[_TT_100]", 2000, 2000, 0.9),
                        (b" T\xe1\xba\xa1m", 2100, 2600, 0.7),
                        (b" bi\xe1\xbb\x87t", 2600, 3900, 0.6),  # overruns the segment end
                    ],
                ),
            ]
        )
    )
    bin_dir = tmp_path / "runtimes" / "whisper" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_name = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    cli_path = bin_dir / cli_name
    args_log = tmp_path / "args.jsonl"
    cli_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        f"with open({str(args_log.as_posix())!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(args) + '\\n')\n"
        "of = args[args.index('-of') + 1]\n"
        "sys.stderr.write('whisper_print_progress_callback: progress =  50%\\n')\n"
        "sys.stderr.flush()\n"
        f"shutil.copyfile({str(fixture.as_posix())!r}, of + '.json')\n"
        "Path(of + '.srt').write_text(\n"
        "    '1\\n00:00:00,000 --> 00:00:01,500\\nSRT không được đọc.\\n\\n',\n"
        "    encoding='utf-8',\n"
        ")\n",
        encoding="utf-8",
    )
    cli_path.chmod(0o755)

    model_dir = tmp_path / "models" / "whisper"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_file = model_dir / WHISPER_GGUF_MODELS["base"].filename
    with model_file.open("wb") as fh:
        fh.truncate(int(WHISPER_GGUF_MODELS["base"].size_bytes))

    def logged_args() -> list[list[str]]:
        return [json.loads(line) for line in args_log.read_text(encoding="utf-8").splitlines()]

    progress: list[float] = []
    work_dir = tmp_path / "cache"
    cfg = AsrConfig(backend="whisper_cpp", model="base", device="cpu", threads=6)
    cues = transcribe_media_to_cues(
        wav,
        cfg,
        work_dir=work_dir,
        data_dir=tmp_path,
        progress_cb=lambda p, _m: progress.append(p),
    )
    # Segments come from the JSON-full output; the SRT written beside it is never read.
    assert [c.text for c in cues] == ["Xin chào các bạn.", "Tạm biệt"]
    assert (cues[1].start_ms, cues[1].end_ms) == (2000, 3500)
    assert (cues[1].speech_start_ms, cues[1].speech_end_ms) == (2100, 3500)
    (args,) = logged_args()
    assert args[args.index("-t") + 1] == "6"
    assert args[args.index("-mc") + 1] == "0"
    assert {"-sns", "-pp", "-ng", "-ojf"} <= set(args)
    assert "--vad" not in args and "-vm" not in args
    # whisper-cli writes into the media cache, never beside the user's file, and cleans up.
    assert Path(args[args.index("-of") + 1]).parent == work_dir
    assert [p.name for p in user_dir.iterdir()] == ["speech.wav"]
    assert [p.name for p in work_dir.iterdir()] == ["asr_segments_cache.json"]
    assert any(0.5 < p < 0.96 for p in progress)

    # With the Silero VAD model installed, whisper-cli runs with --vad -vm <model>.
    vad_spec = AUDIO_MODELS[WHISPER_VAD_MODEL_ID]
    vad_path = model_dir / vad_spec.filename
    with vad_path.open("wb") as fh:
        fh.truncate(vad_spec.size_bytes)
    out_dir = tmp_path / "out"
    segments = asr._run_whisper_cpp(wav, cfg, data_dir=tmp_path, out_dir=out_dir)
    args = logged_args()[-1]
    assert args[args.index("--vad") + 1 :][:2] == ["-vm", str(vad_path)]
    assert not list(out_dir.iterdir())
    # Tokens merge into words (special tokens skipped, split UTF-8 rejoined), clamped to
    # their segment; text-token probabilities give avg_logprob.
    assert [s.words for s in segments] == [
        ((0, 400, "Xin"), (400, 900, "chào"), (900, 1300, "các"), (1300, 1800, "bạn.")),
        ((2100, 2600, "Tạm"), (2600, 3500, "biệt")),
    ]
    assert segments[0].avg_logprob == pytest.approx(
        sum(math.log(p) for p in (0.9, 0.8, 0.9, 0.9, 0.5, 0.5, 0.9)) / 7
    )
    assert segments[1].avg_logprob == pytest.approx((math.log(0.7) + math.log(0.6)) / 2)

    # Timeout kills hung process
    cli_path.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n", encoding="utf-8")
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


def test_faster_whisper_word_timestamps_and_word_accurate_splitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = write_wav_file(np.zeros(16_000 * 10, dtype=np.float32), tmp_path / "words.wav", 16_000)
    seen_kwargs: dict[str, object] = {}

    class _FakeWord:
        def __init__(self, start: float, end: float, word: str) -> None:
            self.start = start
            self.end = end
            self.word = word

    class _FakeSeg:
        def __init__(self, start: float, end: float, text: str, words: list[_FakeWord]) -> None:
            self.start = start
            self.end = end
            self.text = text
            self.words = words

    class _FakeInfo:
        duration = 10.0
        language = "vi"

    class _FakeWhisperModel:
        def __init__(self, model_size: str, **kwargs: object) -> None:
            self.model_size = model_size

        def transcribe(self, path: str, **kwargs: object):
            seen_kwargs.update(kwargs)
            # Clause 1 spoken quickly in 0.25..1.80s, followed by a pause until 3.40s,
            # then clause 2 in 3.40..8.60s (total segment duration 9.0s > 7.0s max_duration_ms).
            words = [
                _FakeWord(0.25, 0.60, "Mệnh"),
                _FakeWord(0.60, 0.95, "đề"),
                _FakeWord(0.95, 1.35, "đầu"),
                _FakeWord(1.35, 1.80, "tiên,"),
                _FakeWord(3.40, 4.20, "mệnh"),
                _FakeWord(4.20, 5.10, "đề"),
                _FakeWord(5.10, 6.50, "thứ"),
                _FakeWord(6.50, 8.60, "hai."),
            ]
            return (
                iter([_FakeSeg(0.0, 9.0, "Mệnh đề đầu tiên, mệnh đề thứ hai.", words)]),
                _FakeInfo(),
            )

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    work_dir = tmp_path / "work"
    cues = transcribe_media_to_cues(
        wav,
        AsrConfig(backend="faster_whisper", model="base", language="vi"),
        work_dir=work_dir,
    )
    assert seen_kwargs.get("word_timestamps") is True
    assert len(cues) == 2
    # Split lands on exact word boundaries (1800 ms, 3400 ms) instead of 50/50 char ratio
    assert cues[0].text == "Mệnh đề đầu tiên,"
    assert cues[0].start_ms == 0
    assert cues[0].end_ms == 1800
    assert cues[0].speech_start_ms == 250
    assert cues[0].speech_end_ms == 1800

    assert cues[1].text == "mệnh đề thứ hai."
    assert cues[1].start_ms == 3400
    assert cues[1].end_ms == 9000
    assert cues[1].speech_start_ms == 3400
    assert cues[1].speech_end_ms == 8600

    # Cached segments preserve word timestamps and speech bounds on reload
    cues_cached = transcribe_media_to_cues(
        wav,
        AsrConfig(backend="faster_whisper", model="base", language="vi"),
        work_dir=work_dir,
    )
    assert cues_cached == cues


def test_openai_api_verbose_json_word_timestamps(tmp_path: Path) -> None:
    wav = write_wav_file(np.zeros(16_000 * 4, dtype=np.float32), tmp_path / "api_words.wav", 16_000)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(length)
            payload = {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": "Xin chào bạn.",
                        "words": [
                            {"word": "Xin", "start": 0.22, "end": 0.55},
                            {"word": "chào", "start": 0.55, "end": 1.05},
                            {"word": "bạn.", "start": 1.05, "end": 1.62},
                        ],
                    }
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
        assert cues[0].start_ms == 0
        assert cues[0].end_ms == 2000
        assert cues[0].speech_start_ms == 220
        assert cues[0].speech_end_ms == 1620
    finally:
        server.shutdown()
        server.server_close()
