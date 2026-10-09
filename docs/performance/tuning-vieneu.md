# VieNeu engine tuning evidence (perf track 7.4)

Bench-gated decisions for the engine knobs added in perf track Phase 7:
ORT session options (7.1), the BLAS thread cap (7.1), export-sized codec
chunks (7.2) and batched PyTorch export (7.3). A default changes only where
this evidence shows a win; every other knob ships at the SDK's behaviour.

Raw evidence: [`evidence/vieneu-tuning-linux-arm64-cpu-onnx-int8.json`](evidence/vieneu-tuning-linux-arm64-cpu-onnx-int8.json)
(per-cell aggregates plus one row per measured run; the 100 ms resource
samples are dropped, everything else is as recorded).

## Decisions

| Knob | Setting / flag | Decision | Evidence |
| --- | --- | --- | --- |
| BLAS thread cap | `blas_threads` | **Default flipped: None → 1** (`a04fe06`) | RTF 1.43 → 0.80, CPU 208 % → 123 %, +130 MB peak RSS |
| ORT intra-op threads | `ort_intra_op_threads` | Keep SDK default (`min(cpu//2, 8)`) | 4 threads ties the default; 1 and 3 are slower |
| Per-step session single thread | `ort_step_single_thread` | Keep off | Helps only at 1 intra thread (still slower than the default); slower or noise elsewhere |
| Spin during job | `ort_spin_during_job` | Keep off | 1.4–2.9× slower at ≥ 2 intra threads; within noise at 1 |
| Export codec chunk frames | `TTSEngine(export_chunk_frames=)` | Keep off (not wired to Settings) | No speed effect, uncapped or capped; +75 MB RSS at 50–100 frames |
| Batched PyTorch export | `TTSEngine(export_batch_size=)` | Keep off (not wired to Settings) | Unmeasured: needs a CUDA host (bead below) |

## Host and method

- Host: `linux-arm64-neoverse-n1-4c`: 4 vCPU Neoverse-N1 (aarch64), 23 GB
  RAM, Linux 6.17, CPython 3.13.14, onnxruntime 1.29.0, numpy 2.5.2
  (scipy-openblas 0.3.34), vieneu 3.3.0. The SDK default here is 2 intra-op
  threads.
- Engine: VieNeu v3 Turbo, ONNX int8 (official pinned model set, fetched by
  the SDK into an isolated `HF_HOME`, then run with `HF_HUB_OFFLINE=1`).
- Workload: `vi_256` corpus entry, `--mode stream`, `--path direct`
  (`run_engine`: the engine alone, no controller or audio transport),
  12.88 s of audio per run.
- Each matrix cell is fresh child processes: 1 cold run (new process: model
  load + first job), then 1 discarded warm-up plus 3 measured warm runs in
  a second process. Medians are over the 3 warm runs.
- RTF = synthesis wall time / audio duration (lower is better; < 1 is
  faster than real time). First chunk = `engine_call_started` →
  `engine_first_chunk`.
- No other CPU-heavy work ran during the sweeps. The host is a shared cloud
  VM, so run-to-run spread is ~±10 %: only effects well outside the min–max
  ranges below count as evidence.

Commands (scripted, unattended; `HW=linux-arm64-neoverse-n1-4c`):

```bash
M=".venv/bin/python -m scripts.benchmarks.run_matrix --engine real --path direct \
  --scenario vi_256 --mode stream --backend onnx --hardware-class $HW \
  --cold-iterations 1 --warm-iterations 3"
$M --threads 1 3 4 --step-single-thread off on --spin off on --output ort.jsonl
$M --step-single-thread off on --spin off on --output ort_default.jsonl
$M --blas-threads 1 2 --output blas.jsonl
$M --export-chunk-frames 25 50 100 --output export.jsonl
.venv/bin/python -m scripts.benchmarks.summarize *.jsonl --output summary.json
```

The BLAS confirmation ran `run_engine --warmup-iterations 1 --iterations 3`
per condition, in two interleaved rounds (default → `OPENBLAS_NUM_THREADS=1`
→ `OMP_NUM_THREADS=1` → all three), so slow drift on the VM could not stand
in for a knob effect.

## Results

### ORT session knobs (BLAS uncapped)

Warm RTF median [min–max]; threads "SDK" = the SDK's own choice (2 here).

| Intra threads | step 1-thread | spin | RTF | first chunk (ms) | CPU % |
| --- | --- | --- | --- | --- | --- |
| SDK | off | off | **1.37** [1.28–1.41] | 1217 | 205 |
| SDK | on | off | 1.57 [1.55–1.60] | 1179 | 189 |
| SDK | off | on | 2.53 [2.45–2.63] | 1309 | 185 |
| SDK | on | on | 2.25 [2.03–2.27] | 1312 | 179 |
| 1 | off | off | 1.81 [1.68–1.98] | 1468 | 170 |
| 1 | on | off | 1.56 [1.53–1.66] | 1311 | 168 |
| 1 | off | on | 1.66 [1.57–1.90] | 1418 | 169 |
| 1 | on | on | 1.87 [1.77–1.88] | 1422 | 159 |
| 3 | off | off | 1.44 [1.40–1.47] | 1131 | 219 |
| 3 | on | off | 1.58 [1.36–1.61] | 1000 | 197 |
| 3 | off | on | 3.22 [3.07–3.41] | 1670 | 189 |
| 3 | on | on | 2.73 [2.54–2.82] | 1369 | 185 |
| 4 | off | off | 1.37 [1.26–1.46] | 886 | 222 |
| 4 | on | off | 1.32 [1.30–1.55] | 1195 | 214 |
| 4 | off | on | 3.96 [3.89–4.14] | 1975 | 192 |
| 4 | on | on | 2.89 [2.85–2.91] | 1903 | 191 |

- **Spin** is the clearest loser: once two or more intra-op threads
  busy-wait, they starve everything else on a 4-core host (1.4–2.9×
  slower), and the cost grows with the thread count. At 1 thread it is
  within noise.
- **Intra threads:** 4 ties the SDK default and 3 is slightly worse; 1
  is ~30 % slower. No case for overriding the SDK.
- **Step-session single thread** helps only with 1 intra thread, a
  configuration nobody should run, and is within noise at 4.
- Cold model load (`engine_initialize`) is 3.5–3.8 s in every cell. Peak
  RSS is 783–790 MB throughout.

### BLAS thread cap (ORT at SDK defaults)

| Condition | RTF median [min–max] | CPU % | peak RSS (MB) |
| --- | --- | --- | --- |
| uncapped (matrix) | 1.37 [1.28–1.41] | 205 | 783 |
| cap 1 (matrix) | **0.79** [0.78–0.80] | 123 | 922 |
| cap 2 (matrix) | 0.85 [0.83–0.87] | 167 | 922 |
| uncapped (confirmation, 6 runs) | 1.43 [1.25–1.63] | 193–226 | 781–796 |
| `OPENBLAS_NUM_THREADS=1` only | 0.80 [0.77–0.81] | 121–125 | 916–919 |
| `OMP_NUM_THREADS=1` only | 0.80 [0.76–0.86] | 116–127 | 917–920 |
| all three = 1 | 0.80 [0.75–0.81] | 122–129 | 917–922 |

Every capped run beat every uncapped one (worst capped 0.86, best uncapped
1.25). The SDK does per-step numpy work between ORT calls. With OpenBLAS
sized to every core, its idle workers spin after each call and fight ORT's
intra-op threads, the same failure as ORT spin above. One BLAS thread removes
the contention: about 40 % less wall time and 85 percentage points less
CPU. `OPENBLAS_NUM_THREADS` alone carries the whole effect (OpenBLAS also
honours `OMP_NUM_THREADS` when its own variable is unset).

Cost: about 130 MB more peak RSS with the cap. It reproduced in every
capped run and is not yet explained. It buys 40 % of synthesis time.

**Shipped as:** `Settings.blas_threads = 1`, applied at the top of
`__main__.main` before numpy loads. It covers `OPENBLAS_NUM_THREADS` and
`MKL_NUM_THREADS` only, because `OMP_NUM_THREADS` also sizes torch's and
ggml's pools. Qwen model hosts do not inherit the app's cap
(`strip_applied_blas_cap` in `host_environment`), so torch and ggml keep
their own thread defaults. Variables the user sets still win and still reach
child processes. `blas_threads: null` in settings opts out.

### Export codec chunk frames (BLAS uncapped)

| `export_chunk_frames` | RTF median [min–max] | first chunk (ms) |
| --- | --- | --- |
| SDK (25) | 1.37 [1.28–1.41] | 1217 |
| 25 | 1.47 [1.45–1.55] | 1394 |
| 50 | 1.45 [1.37–1.47] | 1257 |
| 100 | 1.36 [1.25–1.47] | 1262 |

No effect beyond noise, as 7.2 predicted. The SDK's adaptive lead-in
shrinks chunks to 4/6/8 frames while synthesis trails real time, so the cap
only shapes faster-than-real-time export. Uncapped, this host is slower than
real time.

### Follow-up on the new default (BLAS cap 1)

Once capped, this host runs faster than real time, which is where the export
chunk knob could matter and where thread contention changes. So both sweeps
were repeated with `--blas-threads 1` (same method, one run per command
below):

```bash
$M --blas-threads 1 --output capped_base.jsonl
$M --blas-threads 1 --threads 3 4 --output capped_threads.jsonl
$M --blas-threads 1 --export-chunk-frames 50 100 --output capped_export.jsonl
```

| Cell (BLAS cap 1) | RTF median [min–max] | first chunk (ms) | peak RSS (MB) |
| --- | --- | --- | --- |
| SDK threads, SDK chunks | 0.80 [0.76–0.82] | 827 | 919 |
| 3 intra threads | 0.81 [0.79–0.84] | 878 | 921 |
| 4 intra threads | 0.80 [0.77–0.80] | 844 | 923 |
| export chunk 50 | 0.79 [0.79–0.80] | 979 | 995 |
| export chunk 100 | 0.79 [0.76–0.80] | 892 | 998 |

The SDK's thread choice still ties every override. The larger export chunks
are within noise on speed but add about 75 MB peak RSS, so the knob stays off
on this host. Its remaining case is long-form export on a host far faster
than real time (bead below).

## Unmeasured cells

This host can only measure the CPU/ONNX column, and the decisions above are
evidence for one hardware class. Bead `VieNeuTTSApp-hay8` tracks the rest:

- x86_64 (Windows, Linux), Apple Silicon and >4-core hosts: re-run the ORT
  and BLAS sweeps. The BLAS default is the decision most worth confirming on
  a large-core x86 host, though capping numpy's BLAS under an inference
  runtime is the standard recommendation.
- PyTorch/CUDA backend: `export_batch_size` (7.3) needs a CUDA host. ORT
  knobs do not apply there.
- Long-form export (`vi_2000`, `vi_5000`) for the export knobs on a host
  that is clearly faster than real time.

Re-run with the commands above (add `--backend torch` and
`--export-batch-size 2 4 8` for the CUDA cell). Add the new evidence file
beside this one and update the decision table only where the evidence moves.
