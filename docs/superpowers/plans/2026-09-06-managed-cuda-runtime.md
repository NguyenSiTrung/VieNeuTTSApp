# Managed CUDA Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace oversized bundled CUDA downloads with a user-installed,
verified CUDA runtime while retaining small CPU-only app releases.

**Architecture:** A pinned wheel manifest and a new `CudaRuntimeManager`
download, validate, stage, extract, and atomically promote the PyTorch CUDA
runtime below app data. The controller exposes its lifecycle to Settings; the
engine activates only the managed runtime before CUDA imports. Release CI and
the updater return to a CPU-only artifact matrix.

**Tech Stack:** Python 3.13, stdlib `urllib`/`zipfile`/`hashlib`, PySide6/QML,
PyInstaller, pytest, Ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-06-managed-cuda-runtime-design.md`

**Activation precondition:** Satisfied by GitHub Actions run
[`34040995070`](https://github.com/NguyenSiTrung/VieNeuTTSApp/actions/runs/34040995070):
the official `cp313` CUDA wheel closure extracted into an isolated directory,
then imported `torch 2.8.0+cu128`, `torchaudio 2.8.0+cu128`, and
`transformers 4.57.6` and loaded the platform CUDA library on both Windows x64
and Linux x64 without pip.

## Global Constraints

- Support managed CUDA only on Windows x64 and Linux x64. macOS remains
  ONNX/CPU-only.
- Do not require or invoke `pip`, a shell, or an external Python interpreter.
- Download only exact HTTPS URLs from allowlisted official PyTorch/PyPI hosts;
  verify expected byte counts and SHA-256 before extraction.
- Never import, execute, copy, or persist paths from a locally discovered
  runtime.
- Install only below `platformdirs.user_data_dir("VieNeuTTSApp")`; never modify
  the application bundle or system Python locations.
- CPU startup, ONNX operation, and hardware detection must never import torch.
- All user-visible QML strings use `qsTr` and have English translations.
- Do not commit, push, delete a release, or alter a tag unless the user gives
  explicit authorization.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/vienetts_app/core/cuda_runtime_manifest.py` | Immutable per-platform wheel manifests and validation helpers. |
| `src/vienetts_app/core/cuda_runtime.py` | Runtime states, secure download/extract lifecycle, local diagnostics, activation. |
| `src/vienetts_app/core/engine.py` | Activate a verified runtime before deferred CUDA imports. |
| `src/vienetts_app/core/detector.py` | Probe a ready managed runtime without eagerly importing torch. |
| `src/vienetts_app/ui/controller.py` | Async manager ownership, QML properties, slots, and stale-result guards. |
| `src/vienetts_app/ui/qml/SettingsTab.qml` | Opt-in CUDA runtime card and revised backend guidance. |
| `src/vienetts_app/ui/i18n/vienetts_en.ts` | English translations for new Settings strings. |
| `.github/workflows/release.yml` | Three CPU-only release jobs and upload flow. |
| `packaging/vienetts-app.spec` | CPU-only frozen package, without CUDA collection or marker. |
| `src/vienetts_app/core/updates.py` | CPU platform asset selection only. |
| `scripts/lock_cuda_runtime.py` | Maintainer-only manifest candidate generator. |
| `tests/unit/test_cuda_runtime*.py` | Manager, manifest, discovery, activation, engine/controller behavior. |

### Task 1: Define immutable runtime manifests

**Files:**
- Create: `src/vienetts_app/core/cuda_runtime_manifest.py`
- Create: `tests/unit/test_cuda_runtime_manifest.py`
- Modify: `pyproject.toml` only if the source package discovery needs it.

**Interfaces:**
- Produces `RuntimeWheel(filename, url, size_bytes, sha256)`.
- Produces `CudaRuntimeManifest(format_version, platform_key, python_tag, wheels)`.
- Produces `manifest_for_platform(platform_key: str) -> CudaRuntimeManifest | None`.

- [ ] **Step 1: Write failing manifest tests**

```python
def test_windows_manifest_has_unique_pinned_wheels() -> None:
    manifest = manifest_for_platform("windows-x64")
    assert manifest is not None
    assert manifest.python_tag == "cp313"
    assert len({wheel.filename for wheel in manifest.wheels}) == len(manifest.wheels)
    assert all(wheel.url.startswith("https://download.pytorch.org/") or
               wheel.url.startswith("https://files.pythonhosted.org/")
               for wheel in manifest.wheels)
    assert all(len(wheel.sha256) == 64 and wheel.size_bytes > 0 for wheel in manifest.wheels)
```

Add equivalent Linux coverage and tests that macOS/unknown keys return `None`.

- [ ] **Step 2: Run the test and confirm the import failure**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_manifest.py`

Expected: collection fails because `cuda_runtime_manifest` does not exist.

- [ ] **Step 3: Implement the manifest module**

Define frozen dataclasses and a platform-key map. Populate it from exact
`cp313` wheel URLs, sizes, and SHA-256 values generated by Task 7. Reject a
manifest at module import if a URL host, filename, hash, or size violates the
test contract.

- [ ] **Step 4: Run manifest tests**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_manifest.py`

Expected: all manifest cases pass.

- [ ] **Step 5: Commit when authorized**

```bash
git add src/vienetts_app/core/cuda_runtime_manifest.py tests/unit/test_cuda_runtime_manifest.py
git commit -m "feat(cuda): add pinned runtime manifests"
```

### Task 2: Implement the secure staged runtime manager

**Files:**
- Create: `src/vienetts_app/core/cuda_runtime.py`
- Create: `tests/unit/test_cuda_runtime_manager.py`

**Interfaces:**
- Consumes `CudaRuntimeManifest` and `RuntimeWheel`.
- Produces `CudaRuntimeStatus`, `CudaRuntimeLocation`, and
  `CudaRuntimeManager(root, manifest, downloader, disk_usage)`.
- `inspect()`, `install(cancelled, on_progress)`, `cancel_staging()`, and
  `remove()` never touch an active runtime until complete verification.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_validated_staging_promotes_only_complete_runtime(tmp_path: Path) -> None:
    manager = CudaRuntimeManager(tmp_path, manifest=mini_manifest(), downloader=write_wheel)
    status = manager.install()
    assert status.state == "ready"
    assert status.location is not None
    assert (status.location.root / "install.json").is_file()
    assert not (manager.root / ".staging" / status.location.format_version).exists()

def test_checksum_failure_never_creates_active_runtime(tmp_path: Path) -> None:
    manager = CudaRuntimeManager(tmp_path, manifest=mini_manifest(), downloader=write_bad_wheel)
    assert manager.install().state == "failed"
    assert not list(tmp_path.glob("*/install.json"))
```

Also cover low disk before download, retained verified partial archives after
cancel, retryable HTTP-range resume, corrupt metadata, failed promotion
rollback, and removal refusal while `in_use=True`.

- [ ] **Step 2: Run the manager tests and confirm failure**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_manager.py`

Expected: collection fails because `CudaRuntimeManager` does not exist.

- [ ] **Step 3: Implement exact validation and promotion**

Use `<root>/.staging/<format>/wheels/<filename>.whl.part` for streamed
downloads and `<root>/<format>/site-packages` for the active runtime. Hash
archives before `zipfile.ZipFile` extraction; reject any member that is
absolute, contains `..`, has a symlink mode, or resolves outside the staging
site-packages root. Persist format, platform, Python tag, and wheel hashes in
`install.json`, then use `os.replace` to promote staging with rollback.

- [ ] **Step 4: Run manager tests**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_manager.py`

Expected: all lifecycle, integrity, and cancellation cases pass.

- [ ] **Step 5: Commit when authorized**

```bash
git add src/vienetts_app/core/cuda_runtime.py tests/unit/test_cuda_runtime_manager.py
git commit -m "feat(cuda): add verified runtime installer"
```

### Task 3: Add local diagnostics and isolated activation

**Files:**
- Modify: `src/vienetts_app/core/cuda_runtime.py`
- Modify: `src/vienetts_app/core/engine.py`
- Modify: `src/vienetts_app/core/detector.py`
- Create: `tests/unit/test_cuda_runtime_activation.py`
- Modify: `tests/unit/test_engine.py`
- Modify: `tests/unit/test_detector.py`

**Interfaces:**
- Produces `discover_local_cuda_runtimes(environ, roots) -> list[LocalCudaRuntime]`.
- Produces `activate_cuda_runtime(location) -> RuntimeActivation`.
- `TTSEngine(..., cuda_runtime: CudaRuntimeLocation | None)` activates only for
  resolved backend `"torch"`.

- [ ] **Step 1: Write failing activation tests**

```python
def test_activation_prepends_verified_site_packages_before_factory(monkeypatch, tmp_path):
    location = ready_location(tmp_path)
    seen = []
    engine = TTSEngine(backend="torch", cuda_runtime=location,
                       factory=lambda **kw: seen.append(sys.path[0]) or FakeVieneu())
    engine.initialize()
    assert seen == [str(location.site_packages)]

def test_local_discovery_is_diagnostic_only(tmp_path: Path) -> None:
    found = discover_local_cuda_runtimes(
        environ={"VIRTUAL_ENV": str(tmp_path)}, roots=[make_compatible_layout(tmp_path)]
    )
    assert found[0].compatible is True
    assert str(tmp_path) not in found[0].label
```

Add tests that ONNX/auto detection does not activate torch, Windows DLL
directories are added before imports through an injectable `add_dll_directory`,
and invalid active metadata raises a typed managed-runtime error.

- [ ] **Step 2: Run activation tests and confirm failure**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_activation.py`

Expected: failure because the activation and discovery functions are missing.

- [ ] **Step 3: Implement diagnostics and engine integration**

Scan only `VIRTUAL_ENV`, `CONDA_PREFIX`, `PYTHONPATH`, and injected known
roots. Read wheel/package metadata without executing Python. In
`activate_cuda_runtime`, revalidate `install.json`, prepend its site-packages,
and retain Windows DLL-directory handles on an activation object. Pass an
optional ready location from `AppController._ensure_worker` to `TTSEngine`;
convert missing, corrupted, and driver failures into actionable
`TTSEngineError` text.

- [ ] **Step 4: Run the affected suites**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_activation.py tests/unit/test_engine.py tests/unit/test_detector.py`

Expected: all runtime activation and existing engine/detector tests pass.

- [ ] **Step 5: Commit when authorized**

```bash
git add src/vienetts_app/core/cuda_runtime.py src/vienetts_app/core/engine.py src/vienetts_app/core/detector.py tests/unit/test_cuda_runtime_activation.py tests/unit/test_engine.py tests/unit/test_detector.py
git commit -m "feat(cuda): activate managed runtime for torch"
```

### Task 4: Expose CUDA runtime state through the controller

**Files:**
- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/app.py`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/unit/test_app_entry.py`

**Interfaces:**
- Adds constructor seams `cuda_runtime_manager_factory` and
  `local_cuda_discovery`.
- Adds properties `cudaRuntimeState`, `cudaRuntimeProgress`,
  `cudaRuntimeInstalledBytes`, `cudaRuntimeRequiredBytes`, `cudaRuntimeError`,
  `cudaRuntimeSupported`, `cudaRuntimeReady`, and `localCudaRuntimes`.
- Adds slots `refreshCudaRuntimeState`, `discoverLocalCudaRuntimes`,
  `installCudaRuntime`, `cancelCudaRuntimeInstall`, `removeCudaRuntime`.

- [ ] **Step 1: Write failing controller tests**

```python
def test_cuda_install_discards_stale_completion(qapp, tmp_path: Path) -> None:
    controller = AppController(data_dir=tmp_path, cuda_runtime_manager_factory=FakeFactory())
    controller.installCudaRuntime()
    controller.cancelCudaRuntimeInstall()
    complete_first_background_call(CudaRuntimeStatus("ready"))
    assert controller.cudaRuntimeReady is False
```

Add tests for unsupported platform, no download without an install-slot call,
progress updates, local diagnostic labels, removal restart guidance, and
worker construction receiving the ready runtime.

- [ ] **Step 2: Run controller tests and confirm failure**

Run: `.venv/bin/pytest -q tests/unit/test_controller.py`

Expected: failure because CUDA runtime properties and slots do not exist.

- [ ] **Step 3: Implement asynchronous lifecycle**

Mirror the model manager’s generation counter, `threading.Event` cancellation,
Qt status signal, and injectable `run_on_thread_pool` use. Schedule only
filesystem inspection after first paint in `run_gui`; do not run local
diagnostics or any torch import automatically.

- [ ] **Step 4: Run controller and GUI entry tests**

Run: `.venv/bin/pytest -q tests/unit/test_controller.py tests/unit/test_app_entry.py`

Expected: all existing and new controller contracts pass.

- [ ] **Step 5: Commit when authorized**

```bash
git add src/vienetts_app/ui/controller.py src/vienetts_app/app.py tests/unit/test_controller.py tests/unit/test_app_entry.py
git commit -m "feat(cuda): expose managed runtime controls"
```

### Task 5: Build the opt-in Settings experience

**Files:**
- Modify: `src/vienetts_app/ui/qml/SettingsTab.qml`
- Modify: `src/vienetts_app/ui/i18n/vienetts_en.ts`
- Regenerate: `src/vienetts_app/ui/i18n/vienetts_en.qm`
- Modify: `tests/smoke/test_ui_tabs.py`
- Modify: `tests/unit/test_i18n.py`

**Interfaces:**
- Consumes Task 4 properties and slots.
- Produces stable object names:
  `cudaRuntimeCard`, `cudaRuntimeInstallButton`, `cudaRuntimeCancelButton`,
  `cudaRuntimeRetryButton`, `cudaRuntimeRemoveButton`, and
  `cudaRuntimeDetectLocalButton`.

- [ ] **Step 1: Write failing QML smoke/i18n tests**

```python
assert window.findChild(QObject, "cudaRuntimeCard") is not None
assert window.findChild(QObject, "cudaRuntimeInstallButton").property("enabled") is True
controller._publish_cuda_status(CudaRuntimeStatus("downloading", progress=0.5))
assert window.findChild(QObject, "cudaRuntimeCancelButton").property("visible") is True
```

Add a no-support scenario that hides installation controls, a ready scenario
that exposes removal, and translation assertions for the new Settings labels.

- [ ] **Step 2: Run the smoke/i18n tests and confirm failure**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/smoke/test_ui_tabs.py tests/unit/test_i18n.py`

Expected: object-name and translation assertions fail before the card exists.

- [ ] **Step 3: Implement the card and translations**

Replace guidance to download a separate CUDA build with a card that shows
hardware status, downloaded/required bytes, verified state, explanatory
local-runtime count, and explicit install/cancel/retry/remove controls.
Bind all strings with `qsTr`, update the English catalog, and regenerate the
`.qm` catalog with the project’s existing `pyside6-lrelease` command.

- [ ] **Step 4: Run UI validation**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/smoke/test_ui_tabs.py tests/unit/test_i18n.py`

Expected: all Settings smoke scenarios and catalog tests pass.

- [ ] **Step 5: Commit when authorized**

```bash
git add src/vienetts_app/ui/qml/SettingsTab.qml src/vienetts_app/ui/i18n/vienetts_en.ts src/vienetts_app/ui/i18n/vienetts_en.qm tests/smoke/test_ui_tabs.py tests/unit/test_i18n.py
git commit -m "feat(cuda): add runtime installer settings"
```

### Task 6: Restore CPU-only release and update behavior

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `packaging/vienetts-app.spec`
- Modify: `src/vienetts_app/core/updates.py`
- Modify: `tests/unit/test_updates.py`
- Modify: `tests/unit/test_linux_packaging.py`
- Add or modify: packaging workflow tests beside existing test files.

**Interfaces:**
- `current_platform_key()` returns only Windows/Linux/macOS CPU keys.
- Release matrix contains `windows-x64`, `linux-x64`, and `macos-arm64`.

- [ ] **Step 1: Write failing release/updater tests**

```python
def test_frozen_cuda_marker_does_not_change_download_platform(monkeypatch) -> None:
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates._platform, "machine", lambda: "AMD64")
    assert updates.current_platform_key() == "windows-x64"

def test_release_workflow_has_only_cpu_platforms() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    assert "windows-x64-cuda" not in workflow
    assert "linux-x64-cuda" not in workflow
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `.venv/bin/pytest -q tests/unit/test_updates.py tests/unit/test_linux_packaging.py`

Expected: CUDA platform-selection and workflow assertions fail.

- [ ] **Step 3: Simplify packaging and CI**

Delete the CUDA matrix entries, torch installation step, GPU build environment,
CUDA artifact naming, and `_cuda_build` generation from release code. Remove
CUDA platform constants and matching from the updater. Keep CPU PyInstaller
data collection and all three smoke checks unchanged.

- [ ] **Step 4: Run packaging and update tests**

Run: `.venv/bin/pytest -q tests/unit/test_updates.py tests/unit/test_linux_packaging.py`

Expected: CPU-only release contract passes.

- [ ] **Step 5: Commit when authorized**

```bash
git add .github/workflows/release.yml packaging/vienetts-app.spec src/vienetts_app/core/updates.py tests/unit/test_updates.py tests/unit/test_linux_packaging.py
git commit -m "fix(release): ship CPU-only desktop artifacts"
```

### Task 7: Add a reproducible manifest-lock workflow and documentation

**Files:**
- Create: `scripts/lock_cuda_runtime.py`
- Create or modify: `tests/unit/test_cuda_runtime_lock.py`
- Modify: `README.md`
- Modify: `conductor/tech-stack.md`
- Modify: release notes for the feature version.

**Interfaces:**
- `python scripts/lock_cuda_runtime.py --platform windows-x64 --output /tmp/windows.py`
  emits a reviewable manifest candidate from exact wheel metadata.

- [ ] **Step 1: Write failing lock-script tests**

```python
def test_lock_script_rejects_non_allowlisted_url(tmp_path: Path) -> None:
    result = run_lock_script(fake_index={"torch": "https://example.invalid/torch.whl"})
    assert result.returncode != 0
    assert "allowlisted" in result.stderr
```

Add a fixture-driven successful resolution test proving the generated wheel
records are sorted and include URL, filename, size, and SHA-256.

- [ ] **Step 2: Run the test and confirm failure**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_lock.py`

Expected: failure because the lock script does not exist.

- [ ] **Step 3: Implement the maintainer-only generator**

Use pip’s JSON report capability only in this maintenance script to resolve
the pinned top-level versions against official indexes, download candidate
wheels, calculate SHA-256 and sizes, and print Python manifest records. The
application runtime never invokes this script or pip.

- [ ] **Step 4: Document the user and maintainer flows**

Document CPU-only downloads, the explicit in-app CUDA download, supported
platforms, required NVIDIA driver, offline behavior after installation, local
diagnostic semantics, removal/restart behavior, and the manifest update
command. State that the app does not execute a locally detected Python runtime.

- [ ] **Step 5: Run script, docs, and tests**

Run: `.venv/bin/pytest -q tests/unit/test_cuda_runtime_lock.py && .venv/bin/ruff check . && .venv/bin/ruff format --check .`

Expected: lock-script tests and style checks pass.

- [ ] **Step 6: Commit when authorized**

```bash
git add scripts/lock_cuda_runtime.py tests/unit/test_cuda_runtime_lock.py README.md conductor/tech-stack.md packaging/release-notes
git commit -m "docs(cuda): document managed runtime install"
```

### Task 8: Run full verification and remediate the failed release

**Files:**
- Modify only version and release-note files needed for the next feature
  version after `v0.1.10`.

**Interfaces:**
- Consumes all completed runtime and CPU-only release contracts.
- Produces one successful three-platform GitHub Release with CPU artifacts
  below 2 GiB.

- [ ] **Step 1: Run the full local gate**

Run:

```bash
QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest -q --ignore=tests/smoke/test_performance_harness.py
.venv/bin/ruff check .
.venv/bin/ruff format --check .
git diff --check
```

Expected: all tests pass, Ruff reports no violations, and Git reports no
whitespace errors.

- [ ] **Step 2: Build and smoke-test the CPU package locally**

Run:

```bash
VERSION=0.1.11 python -m PyInstaller packaging/vienetts-app.spec --noconfirm --distpath dist --workpath /tmp/vienetts-pyi
dist/VieNeuTTS.app/Contents/MacOS/VieNeuTTS --version
```

On non-macOS builders, substitute the matrix binary path from
`.github/workflows/release.yml`. Confirm no frozen `torch` package or
`_cuda_build` marker exists.

- [ ] **Step 3: Obtain explicit authorization for outward actions**

Ask the user before deleting the empty `v0.1.10` GitHub Release record,
creating a new release commit/tag, or pushing to GitHub. Do not delete the
`v0.1.10` Git tag.

- [ ] **Step 4: Delete only the empty Release record when authorized**

Run:

```bash
gh release delete v0.1.10 --repo NguyenSiTrung/VieNeuTTSApp --yes
```

Verify `gh release view v0.1.10 --repo NguyenSiTrung/VieNeuTTSApp` fails while
`git ls-remote --tags origin refs/tags/v0.1.10` still returns the tag.

- [ ] **Step 5: Publish the feature release when authorized**

Create the next version commit and tag, push both, then monitor the Release
workflow. Confirm the Windows CPU ZIP, Linux CPU ZIP, and macOS ARM64 DMG are
attached to the resulting GitHub Release and each asset is below 2 GiB.

## Plan Self-Review

- **Spec coverage:** Tasks 1–3 implement manifests, integrity, extraction,
  diagnostics, activation, and engine isolation. Tasks 4–5 implement async
  controller and Settings behavior. Task 6 removes oversized CUDA releases.
  Task 7 documents and reproduces manifest generation. Task 8 validates and
  remediates release state.
- **No-placeholder scan:** The plan has concrete interfaces, test commands,
  expected failures, and release commands. It deliberately contains no
  deferred implementation markers.
- **Type consistency:** `RuntimeWheel`, `CudaRuntimeManifest`,
  `CudaRuntimeLocation`, `CudaRuntimeStatus`, `CudaRuntimeManager`,
  `discover_local_cuda_runtimes`, and `activate_cuda_runtime` are introduced
  before use by later tasks.
