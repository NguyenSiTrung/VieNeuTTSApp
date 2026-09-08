"""Opt-in real-model Qwen smoke (Phase 5 Task 4).

Runs ONLY with ``VIENEUTTS_QWEN_REALMODEL=1`` on a machine that has the
optional pack installed (``pip install "vienetts-app[qwen]"`` + cached
checkpoints via ``scripts/fetch_qwen_models.py``). Otherwise every test
skips with the reason — CI and torch-free checkouts never touch this.

When enabled it loads the real checkpoints through the production
registry path, synthesizes short English/Chinese samples behind the
streaming contract, and writes load / first-chunk / RSS observations to
JSONL. Assertions are structural (rate, dtype, shape, finiteness) —
never machine-specific exact timings.
"""

import json
import os
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")

from vienetts_app.core.backends import QWEN_BASE, QWEN_CUSTOMVOICE  # noqa: E402
from vienetts_app.core.qwen_backend import (  # noqa: E402
    QwenBackend,
    load_qwen_model,
    register_default_qwen_backends,
)
from vienetts_app.core.qwen_runtime import QWEN_NATIVE_RATE, require_qwen  # noqa: E402
from vienetts_app.core.tts_backend import assert_backend_contract  # noqa: E402

OPT_IN = os.environ.get("VIENEUTTS_QWEN_REALMODEL") == "1"
needs_realmodel = pytest.mark.skipif(not OPT_IN, reason="opt-in: set VIENEUTTS_QWEN_REALMODEL=1")


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return float("nan")
    return float("nan")


@needs_realmodel
class TestQwenRealModel:
    def test_customvoice_contract_and_observations(self, tmp_path) -> None:
        try:
            require_qwen()
        except Exception as exc:  # noqa: BLE001 - optional pack absent → skip, not fail
            pytest.skip(f"qwen runtime not installed: {exc}")
        register_default_qwen_backends()
        rss_before = _rss_mb()
        started = time.monotonic()
        try:
            backend = load_qwen_model(QWEN_CUSTOMVOICE)
        except Exception as exc:  # noqa: BLE001 - checkpoint absent → skip, not fail
            pytest.skip(f"qwen CustomVoice checkpoint unavailable: {exc}")
        load_s = time.monotonic() - started
        assert isinstance(backend, QwenBackend)
        assert backend.native_sample_rate == QWEN_NATIVE_RATE
        assert_backend_contract(backend, voice="Ryan", language="en", probe_text="hello world")
        first_s: float | None = None
        chunks = 0
        started = time.monotonic()
        for chunk in backend.synthesize_stream("你好世界", voice="Xiaoxiao", language="zh"):
            if first_s is None:
                first_s = time.monotonic() - started
            assert chunk.dtype == np.float32 and chunk.ndim == 1 and chunk.size > 0
            assert np.all(np.isfinite(chunk))
            chunks += 1
        assert chunks >= 1
        record = {
            "engine": QWEN_CUSTOMVOICE,
            "native_rate": QWEN_NATIVE_RATE,
            "load_s": load_s,
            "first_chunk_s": first_s,
            "chunks": chunks,
            "rss_before_mb": rss_before,
            "rss_after_mb": _rss_mb(),
        }
        out = tmp_path / "qwen-realmodel.jsonl"
        out.write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert out.is_file()

    def test_base_requires_reference(self, tmp_path) -> None:
        try:
            require_qwen()
        except Exception as exc:  # noqa: BLE001 - optional pack absent → skip, not fail
            pytest.skip(f"qwen runtime not installed: {exc}")
        ref_audio = os.environ.get("VIENEUTTS_QWEN_REF_AUDIO", "")
        ref_text = os.environ.get("VIENEUTTS_QWEN_REF_TEXT", "")
        if not ref_audio or not ref_text:
            pytest.skip("set VIENEUTTS_QWEN_REF_AUDIO/VIENEUTTS_QWEN_REF_TEXT for the Base probe")
        backend = load_qwen_model(QWEN_BASE, ref_audio=ref_audio, ref_text=ref_text)
        assert backend.native_sample_rate == QWEN_NATIVE_RATE
        out = list(backend.synthesize_stream("hello world", language="en"))
        assert out and all(c.dtype == np.float32 and c.ndim == 1 for c in out)
        backend.close()
