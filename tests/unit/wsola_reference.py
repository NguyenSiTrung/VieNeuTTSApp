"""The pre-Task-5.1 WSOLA, kept verbatim as the parity oracle (test support).

``core.audio.time_stretch_audio`` replaced the per-frame direct convolution
with an FFT correlation over the same search window; this copy is what its
output is held to (length exact, RMS error bound, click-free joins).
"""

from __future__ import annotations

import numpy as np

from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, _validate_mono


def reference_time_stretch(
    audio: np.ndarray,
    rate: float,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """Time-stretch 1-D mono float32 audio preserving pitch using WSOLA.

    Uses canonical Waveform Similarity Overlap-Add (WSOLA) in the time domain,
    eliminating STFT phase vocoder artifacts (no 80 Hz frame buzz, no robotic
    phasiness, no sub-bass rumble). Bypasses processing when ``abs(rate - 1.0) < 1e-3``
    to preserve original audio bit-identically with zero overhead.
    """
    mono = _validate_mono(audio)
    if rate <= 0.0:
        raise ValueError(f"rate must be positive, got {rate}")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
    if abs(rate - 1.0) < 1e-3:
        return mono

    n_in = mono.size
    if n_in == 0:
        return mono
    if n_in < 256:
        target_len = max(1, int(round(n_in / rate)))
        if target_len == n_in:
            return mono
        indices = np.linspace(0, n_in - 1, target_len, endpoint=True)
        return np.interp(indices, np.arange(n_in), mono).astype(np.float32)
    # 25 ms frame is the standard analysis frame for human speech pitch periods (80 - 350 Hz)
    frame_len = int(sample_rate * 0.025)
    if frame_len % 2 != 0:
        frame_len += 1
    if n_in < frame_len * 2:
        frame_len = max(32, (n_in // 3) & ~1)

    s_syn = frame_len // 2
    s_ana = int(round(s_syn * rate))
    delta_max = min(frame_len // 2, int(sample_rate * 0.015))

    # Tukey window with 50% flat top retains unaltered speech in the middle
    n_win = np.arange(frame_len, dtype=np.float32)
    taper_len = frame_len // 4
    win = np.ones(frame_len, dtype=np.float32)
    if taper_len > 0:
        left_taper = 0.5 * (1.0 - np.cos(np.pi * n_win[:taper_len] / taper_len))
        win[:taper_len] = left_taper
        win[-taper_len:] = left_taper[::-1]

    target_len = int(round(n_in / rate))
    n_frames = int(np.ceil(target_len / s_syn)) + 2
    out_buf_len = (n_frames + 2) * s_syn + frame_len
    output = np.zeros(out_buf_len, dtype=np.float32)
    norm = np.zeros(out_buf_len, dtype=np.float32)

    delta_prev = 0
    tau_prev = 0
    output[:frame_len] += mono[:frame_len] * win
    norm[:frame_len] += win

    curr_syn = s_syn
    curr_ana = s_ana

    for _ in range(1, n_frames):
        target_pos = tau_prev + delta_prev + s_syn
        if target_pos + frame_len > n_in:
            break

        target_seg = mono[target_pos : target_pos + frame_len]
        nominal_pos = curr_ana
        start_search = max(0, nominal_pos - delta_max)
        end_search = min(n_in - frame_len, nominal_pos + delta_max)

        if start_search >= end_search:
            best_pos = max(0, min(n_in - frame_len, nominal_pos))
        else:
            search_region = mono[start_search : end_search + frame_len]
            corrs = np.convolve(search_region, target_seg[::-1], mode="valid")
            sq = search_region**2
            csum = np.cumsum(np.pad(sq, (1, 0)))
            cand_energies = (
                csum[frame_len : len(search_region) + 1]
                - csum[: len(search_region) + 1 - frame_len]
            )
            denom = np.sqrt(np.maximum(cand_energies, 1e-8)) * np.sqrt(
                np.maximum(np.sum(target_seg**2), 1e-8)
            )
            norm_corrs = corrs / denom
            best_idx = int(np.argmax(norm_corrs))
            best_pos = start_search + best_idx

        delta_prev = best_pos - nominal_pos
        tau_prev = nominal_pos

        out_start = curr_syn
        out_end = out_start + frame_len
        output[out_start:out_end] += mono[best_pos : best_pos + frame_len] * win
        norm[out_start:out_end] += win

        curr_syn += s_syn
        curr_ana = int(round(curr_syn * rate))
        if curr_syn >= target_len:
            break

    valid_norm = norm > 1e-4
    output[valid_norm] /= norm[valid_norm]
    res = output[:target_len]

    # 2 ms micro-fade at buffer edges prevents boundary clicks during chunk concatenation
    fade_len = min(len(res) // 4, int(sample_rate * 0.002))
    if fade_len > 1:
        fade = 0.5 * (1.0 - np.cos(np.linspace(0, np.pi, fade_len, dtype=np.float32)))
        res[:fade_len] *= fade
        res[-fade_len:] *= fade[::-1]

    return np.ascontiguousarray(res, dtype=np.float32)
