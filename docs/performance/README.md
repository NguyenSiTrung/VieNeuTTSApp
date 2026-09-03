# Performance evidence

This directory contains local, content-safe evidence for the VieNeuTTS
performance and resource work. Stage 1 is measurement only. It does not
change synthesis ordering, cancellation, buffering, backend selection, or
model loading defaults.

## Metric definitions

All event timestamps are monotonic nanosecond offsets from the trace start;
derived latency metrics use the explicit `submitted` event as their origin.

```text
TTFC = submitted to worker_first_chunk
controller first chunk = submitted to controller_first_chunk
first transport append = submitted to audio_first_buffer_append
first sink pull = submitted to audio_first_sink_pull
audible TTFA = external loopback only, not inferred from sink pull
```

Worker completion means the inference worker emitted its terminal audio
signal. Sink pull means the audio sink requested bytes from the transport
device. Neither is proof that sound reached a speaker.

Resource records include current RSS samples, peak RSS, process CPU time, and
the full raw sample list. CUDA records, when supported, include allocated,
reserved, maximum allocated, and maximum reserved allocator bytes.

## Artifact-first synthesis and playback bounds

Normal synthesis writes validated 48 kHz mono WAV artifacts incrementally.
The worker emits only job/chunk metadata and a terminal artifact descriptor;
it does not transfer or retain a duration-sized PCM array. Interactive
artifacts are owned by the application until replay/export releases them.
Audiobook artifacts are copied to a chapter-local `.part.wav`, validated, then
atomically promoted to the chapter cache before their managed source is
released. Failed, cancelled, invalid, and stale renders do not publish a
chapter WAV.

Optional live playback uses a bounded PCM transport with a strict two-second
maximum (384,000 bytes at 48 kHz mono float32). If an output device is
unavailable, artifact creation and WAV export remain available; no audible
real-time claim is implied by that fallback.

Benchmark records include `artifact_bytes_on_disk` and
`transport_max_bytes`. Report these counters with the raw record rather than
inferring a real-time target from them.

## Cold, warm, direct, and pipeline runs

- **Process-cold startup** (`process_cold_startup_ms`) launches one fresh
  child process per observation. The parent owns the process-start timestamp
  (`process_start_parent_ns`); the child reports `qml_loaded`,
  `window_exposed`, and `first_frame_swapped` plus `frame_signal_supported`.
  The cold figure spans parent spawn through the child's first-frame or
  exposure milestone. Never reuse a `QGuiApplication`, process, temporary
  model state, or model manager between iterations.
- **Page-cache-warm** reuses warm OS file pages without a loaded engine.
- **Model-warm** reuses a loaded engine in the same process.
- **In-process QML boot** (`in_process_qml_boot_ms`) spans child start through
  the child's own QML load milestone. It separates child-side construction
  from parent-side spawn overhead.
- **Direct engine** calls `TTSEngine` without the worker, controller, or audio
  sink. Its first chunk is an SDK/engine metric, not audible TTFA.
- **Production pipeline** uses the real worker, controller, and stream
  transport. Fake sinks are useful for reproducible transport comparisons.
- **Real sink** is device-dependent and must be reported as failed or
  unsupported when no supported output device exists.
- The model downloader is excluded from startup metrics.
- No benchmark record may contain personal file paths or source text.

Compare raw samples and distributions by the same path, scenario, backend,
precision, sink kind, and cold/warm class. Do not select a best-of-N value as
the product claim. Use the median, p90, p95, and median absolute deviation
(MAD), while retaining the raw JSONL samples.

## Commands

Run the deterministic plumbing smoke benchmark:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_once \
  --engine fake --scenario vi_50 --mode stream \
  --sink fake --output /tmp/vienetts-fake-record.jsonl
```

Run a matrix with fresh processes:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_matrix \
  --engine fake --scenario vi_50 vi_512 \
  --mode stream --path pipeline --sink fake \
  --hardware-class fake-ci \
  --cold-iterations 2 --warm-iterations 3 \
  --output /tmp/vienetts-fake-matrix.jsonl
```

Summarize raw records:

```bash
.venv/bin/python -m scripts.benchmarks.summarize \
  /tmp/vienetts-fake-matrix.jsonl \
  --output /tmp/vienetts-fake-summary.json
```

For a release-lab real run, use an explicit backend and precision:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_matrix \
  --engine real --scenario vi_50 vi_512 \
  --mode stream --path pipeline --sink fake \
  --backend onnx --precision int8 \
  --hardware-class apple-m4-10c-16gb \
  --cold-iterations 5 --warm-iterations 20 \
  --output docs/performance/baselines/pipeline.jsonl
```

The QML runner measures real shell frame intervals and event-loop delay:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_ui \
  --engine fake --scenario vi_512 \
  --hardware-class fake-ci \
  --output /tmp/vienetts-fake-ui.jsonl
```

Timing-sensitive suites run serially with `pytest -n 0`.

## JSONL and safe-data policy

Each line is one versioned JSON object. Records contain:

- `schema_version`, command version, package versions, OS and architecture;
- a caller-supplied capability label such as `apple-m4-10c-16gb`;
- corpus ID, UTF-8 character count, language class, and SHA-256;
- requested settings, event offsets, numeric maxima/counters, resources, and
  derived durations.

Records never contain corpus text, voice names, file paths, audio, hostnames,
usernames, serial numbers, hardware UUIDs, environment variables, or saved
login/session data. Hardware class labels describe capability and never a
unique device. Write outputs only to a user-selected local path.

## RSS and platform support

The resource helper exposes bytes at its API boundary:

- Linux parses `VmRSS` and `VmHWM` from `/proc/<pid>/status` and converts KiB
  to bytes.
- macOS samples current RSS with `ps -o rss=` (KiB) and peak RSS from
  `resource.getrusage`, which reports bytes on macOS.
- Windows uses `GetProcessMemoryInfo` through `ctypes`, including current and
  peak working-set bytes.

Both current and peak RSS are required. Peak-only evidence cannot distinguish
allocator growth from a live retained-memory increase.

## Supported and unsupported measurements

Event-loop delay is numeric and available whenever the Qt timer produces
samples. Frame interval and real-sink measurements are explicitly marked
unsupported when the platform or audio environment cannot provide them.
Audible TTFA is unsupported by these runners and requires external loopback
measurement. Missing metrics remain absent with a `missing_count`; they are
never replaced with zero.

Real-model baselines must be run without unrelated CPU-heavy processes. If
models are missing or a sink is unavailable, preserve the command and error in
the lab report, do not generate synthetic evidence, and leave the
model-dependent baseline file absent.
