# Managed CUDA Runtime

**Date:** 2026-09-06
**Beads:** `VieNeuTTSApp-j1r`
**Status:** Approved design (chat), awaiting spec review

## Goal

Keep all released app installers CPU-only, then let Windows and Linux users
opt into an app-managed CUDA runtime from Settings. The runtime must work
without a separate Python or pip installation, remain offline after a
verified install, and never load unverified packages from a user's machine.

## Context

The v0.1.10 release build matrix successfully built and smoke-tested every
platform, but GitHub Releases rejected the CUDA assets because its per-asset
limit is 2 GiB. The generated bundles were 3.9 GB on Windows and 4.9 GB on
Linux. Splitting these downloads would preserve the limit but would make
installation and in-app updates significantly worse.

The app already has a strong model-install pattern: a pinned manifest,
checksum verification, staging-only writes, free-space preflight, cancellation,
atomic promotion, and a Qt background-runner seam. The CUDA runtime follows
those contracts, but remains a separate concern from model weights.

## Activation Spike Evidence

The disposable GitHub Actions spike run
[`34040995070`](https://github.com/NguyenSiTrung/VieNeuTTSApp/actions/runs/34040995070)
validated the technical boundary on 2026-09-06. It downloaded the official
resolved CUDA wheel closure, extracted it into an isolated `site-packages`
directory without pip, then imported `torch`, `torchaudio`, and `transformers`.

- Windows x64 loaded `torch 2.8.0+cu128`, `torchaudio 2.8.0+cu128`,
  `transformers 4.57.6`, CUDA 12.8, and `c10_cuda.dll`.
- Linux x64 loaded the same package versions and CUDA 12.8, and
  `libc10_cuda.so`.

The spike establishes that the frozen CPython 3.13 runtime can activate a
verified wheel runtime from app data. It does not test GPU inference, which
remains unavailable on GitHub-hosted runners.

## Decisions

- Ship only the existing CPU desktop artifacts for Windows, Linux, and macOS.
- Support managed CUDA only on Windows x64 and Linux x64. macOS stays ONNX/CPU.
- Install the CUDA runtime only after explicit user action. Selecting the
  PyTorch backend never starts a download by itself.
- Download all wheels directly from official PyTorch and PyPI endpoints, using
  a committed, exact-file SHA-256 manifest. No oversized GitHub Release asset
  is needed.
- Detect compatible local Python/CUDA installations only for diagnostic UI.
  Never import, execute, copy, or trust those packages.
- Extract verified wheel files into the per-user app-data directory. Never
  write to the application bundle or system Python directories.
- Activate the managed runtime only immediately before a CUDA engine
  initialization. Once native torch libraries load, removing/deactivating the
  runtime requires an app restart.

## Architecture

### Pinned runtime manifest

Add `core/cuda_runtime_manifest.py`. It contains immutable, reviewed
`CudaRuntimeManifest` values for `windows-x64` and `linux-x64`, each with:

```python
@dataclass(frozen=True)
class RuntimeWheel:
    filename: str
    url: str
    size_bytes: int
    sha256: str

@dataclass(frozen=True)
class CudaRuntimeManifest:
    format_version: str
    platform_key: Literal["windows-x64", "linux-x64"]
    python_tag: str                 # cp313, matching the frozen interpreter
    wheels: tuple[RuntimeWheel, ...]
```

The manifest pins the complete wheel closure needed by the PyTorch backend,
not merely the three top-level packages (`torch==2.8.0`,
`torchaudio==2.8.0`, `transformers==4.57.6`). It includes all
platform-specific transitive wheels, their immutable direct URLs, byte counts,
and SHA-256 digests. The manifest is generated with a documented CI script
from official indexes, then reviewed and committed. Runtime code never
resolves versions or executes pip.

`required_free_bytes` is the sum of all archives plus extraction headroom.
The UI displays the computed download and disk requirements before the user
confirms.

### `CudaRuntimeManager`

Add `core/cuda_runtime.py` with a separate `CudaRuntimeManager`. It mirrors
the tested `ModelManager` lifecycle but uses only the stdlib for direct wheel
retrieval and ZIP extraction:

```python
CudaRuntimeStatus(
    state="unavailable" | "checking" | "downloading" | "validating"
          | "ready" | "failed",
    platform_key="windows-x64" | "linux-x64" | "",
    installed_bytes=0,
    required_bytes=0,
    progress=0.0,
    error="",
    location=CudaRuntimeLocation | None,
)
```

Install layout:

```text
<app data>/runtime/cuda/
  .staging/<manifest format>/
    wheels/<filename>.whl.part
    site-packages/
    install.json
  <manifest format>/
    site-packages/
    install.json
  <manifest format>.previous/
```

For each wheel, the manager:

1. Checks for an already verified archive or extracted active runtime.
2. Streams to its staging archive, resuming only when the partial byte count
   and HTTP range response are valid.
3. Validates byte count and SHA-256 before extraction.
4. Validates every ZIP member path remains within `site-packages`; rejects
   absolute paths, traversal, symlinks, and duplicate package roots.
5. Extracts into staging, records verified package metadata without user paths,
   then atomically promotes the complete directory.

Cancellation keeps valid staged archives for a later resume but never creates
an active runtime. Failed validation cleans only invalid staging data.
Uninstall removes the active managed runtime only after the user confirms and
only when no CUDA engine is loaded; otherwise it asks for an app restart.

### Local-install detection

`discover_local_cuda_runtimes()` performs a read-only, explicit user-requested
diagnostic scan. It checks the frozen process's declared environment locations
(`VIRTUAL_ENV`, `CONDA_PREFIX`, and `PYTHONPATH`) plus standard interpreter
site-package roots, without recursively scanning arbitrary drives or launching
other Python executables.

A candidate is displayed only if it has matching Python `cp313` package files,
the expected package/version metadata, and a platform-compatible torch wheel
layout. Its UI result contains an opaque label and compatibility reason, not
the full local path. A match is advisory only:

- no local package is imported or executed;
- no local package is copied into app storage;
- the normal install button still installs the checked, app-managed manifest.

This check answers whether the user already has CUDA dependencies while
preserving the frozen app's ABI, native-library, and integrity guarantees.

### Runtime activation and engine behavior

Add a small `activate_cuda_runtime(location)` seam in `core/cuda_runtime.py`.
It validates `install.json`, prepends the managed `site-packages` to
`sys.path`, and on Windows retains `os.add_dll_directory()` handles for
`torch/lib` and other declared native-library locations before any torch
import. On Linux it validates the runtime's expected shared-library layout and
relies on the wheel's loader metadata; failures surface as a managed-runtime
error, not an import fallback.

`TTSEngine` calls activation only when its resolved backend is `"torch"`, and
only before deferred imports of `vieneu` can import torch. The engine receives
an injected `CudaRuntimeLocation` from the controller's backend resolution.
There is no global activation during startup, hardware detection, or ONNX
operation.

When the selected backend is `"torch"`:

- a ready managed runtime plus a usable NVIDIA driver enables CUDA;
- no managed runtime produces a typed, user-facing “install CUDA runtime or
  switch to ONNX” error;
- a runtime import or driver error reports the failing prerequisite and leaves
  the CPU baseline untouched.

`"auto"` remains conservative: it uses ONNX unless the managed runtime is
ready and the existing hardware probe confirms usable CUDA. It never downloads
or imports torch to decide.

### Controller and Settings UI

`AppController` gains a dedicated manager factory, diagnostic discovery seam,
and NOTIFY-backed properties:

- `cudaRuntimeState`, `cudaRuntimeProgress`, `cudaRuntimeInstalledBytes`,
  `cudaRuntimeRequiredBytes`, `cudaRuntimeError`;
- `cudaRuntimeSupported`, `cudaRuntimeReady`, `cudaRuntimeChecking`;
- `localCudaRuntimes` (diagnostic labels and compatibility state only).

Slots:

- `refreshCudaRuntimeState()`
- `discoverLocalCudaRuntimes()`
- `installCudaRuntime()`
- `cancelCudaRuntimeInstall()`
- `removeCudaRuntime()`

All installation, checksum work, and discovery run through the existing
injectable background-runner pattern. Generational state guards discard stale
callbacks after cancel, retry, shutdown, or platform changes.

`SettingsTab.qml` replaces the current “download the CUDA build” guidance with
a CUDA runtime card, shown only on supported Windows/Linux hardware:

- driver/hardware and managed-runtime status;
- detected local-runtime count with a “Check local installations” action;
- explicit install button, download/disk-size disclosure, progress, cancel,
  retry, and error message;
- remove button for a ready inactive runtime;
- restart guidance when a loaded CUDA runtime cannot be unloaded.

All new visible strings use `qsTr` and are translated in existing English
catalogs. Selecting the PyTorch backend remains possible, but shows the
install action rather than silently falling back to ONNX.

### Release pipeline and updater

Remove `windows-x64-cuda` and `linux-x64-cuda` jobs from
`.github/workflows/release.yml`, the CUDA-specific torch installation step,
and GPU collection/marker behavior from `packaging/vienetts-app.spec`.
Release artifacts are therefore Windows CPU ZIP, Linux CPU ZIP, and macOS
DMG only.

Remove CUDA asset keys and marker-based selection from `core/updates.py`.
The updater continues to choose the normal CPU artifact by platform.

Add `scripts/lock_cuda_runtime.py` for maintainers. It produces a candidate
manifest from the pinned top-level packages and official package indexes; CI
tests parse/validate the committed manifest but do not redownload multi-GB
packages on every release. A documented maintainer workflow validates a
candidate manifest on each supported OS before it is committed.

## Error handling

- Unsupported OS/architecture: hide install controls and explain CUDA support
  is Windows/Linux x64 only.
- No NVIDIA driver/GPU: disable installation with driver guidance; the local
  diagnostic check remains available.
- Offline/HTTP/range failure: retain only verified staged archives, report a
  retryable error, and keep ONNX usable.
- Hash, size, or archive-layout mismatch: reject the runtime, delete the bad
  staging archive, and show an integrity failure.
- Insufficient disk: fail before network access, with exact required/free
  values.
- CUDA driver / torch import failure after installation: keep the verified
  runtime, report the specific failure, and offer ONNX fallback.
- User cancellation or stale background result: no active runtime is promoted.

## Testing

### Unit

- Manifest is complete, URL scheme/host allowlisted, filenames unique, hashes
  valid hex, sizes positive, and platform/Python tags match the frozen build.
- `CudaRuntimeManager` inspection, preflight, resume, cancellation, checksum
  rejection, ZIP traversal/symlink rejection, extraction, atomic promotion,
  rollback, uninstall, and no-user-path metadata.
- Runtime activation order: `sys.path` and Windows DLL directories are set
  before torch import; ONNX never activates the runtime.
- Local discovery finds compatible/incompatible mock environment roots without
  executing an interpreter or exposing paths in persistent state.
- Controller transitions, stale-result guards, restart semantics, and
  torch/auto backend resolution.
- Update parsing accepts only CPU release asset keys.

### Smoke and packaging

- Settings smoke scenarios cover unavailable, installing, failed, ready,
  local-detection, and removal states.
- A platform-gated integration check validates one minimal managed runtime
  activation against a real CPython 3.13 CUDA wheel layout, without requiring
  a GPU inference.
- PyInstaller CPU build smoke confirms torch is absent from every shipped
  package, and the frozen app still runs ONNX smoke synthesis.
- Release workflow confirms exactly three desktop assets and successfully
  publishes them under GitHub's asset limit.

## Files expected to change

| File | Change |
| --- | --- |
| `core/cuda_runtime_manifest.py` | New pinned runtime manifests. |
| `core/cuda_runtime.py` | New verified runtime manager, local discovery, activation. |
| `core/engine.py` | Activate managed runtime before CUDA imports; typed errors. |
| `core/detector.py` | Conservative driver/readiness probe integration. |
| `ui/controller.py` | Runtime lifecycle, QML properties/slots, injected seams. |
| `ui/qml/SettingsTab.qml` | CUDA runtime card and revised backend guidance. |
| `ui/i18n/*` | Vietnamese/English strings. |
| `core/updates.py` | Remove CUDA release-asset and marker behavior. |
| `packaging/vienetts-app.spec` | Remove GPU collection and CUDA marker. |
| `.github/workflows/release.yml` | Remove oversized CUDA jobs; retain CPU release matrix. |
| `scripts/lock_cuda_runtime.py` | Maintainer manifest-lock utility. |
| `tests/unit/test_cuda_runtime*.py` | New manager, manifest, activation, and controller tests. |
| `tests/unit/test_updates.py`, packaging/UI smoke tests | Updated CPU-only release and UI contracts. |
| README, product/release documentation | Explain opt-in CUDA runtime and changed downloads. |

## Release remediation

`v0.1.10` has a published GitHub Release record but no assets because the
first CUDA upload exceeded GitHub's 2 GiB limit. Delete that empty release
record before the next feature release, preserving its Git tag for audit
traceability. Do not rewrite the tag.

The managed-runtime feature ships as a new version later than v0.1.10. The
next release workflow must complete its three CPU-only build jobs and publish
all assets before the Beads release-blocker issue is closed.

## Non-goals

- No arbitrary package manager or generic Python environment support.
- No automatic download in response to hardware detection or backend choice.
- No importing/copying executable code from a user’s existing local runtime.
- No CUDA support on macOS, ARM Linux/Windows, AMD, Intel, or other GPU APIs.
- No reintroduction of multi-gigabyte GitHub Release assets.
