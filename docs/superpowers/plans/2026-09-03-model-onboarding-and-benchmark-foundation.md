# Model Onboarding and Benchmark Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a clean packaged installation download, verify, and use the
official CPU baseline model offline, while correcting the startup benchmark so
its measurements represent separate application processes.

**Architecture:** `ModelManager` owns a versioned, app-data-resident official
model install. It downloads a fixed allowlist to a staging directory, validates
sizes and SHA-256 checksums, then atomically promotes it to a local
`backbone/` plus `codec/` layout consumed by `TTSEngine`. The user-facing
controller runs all network and full-file verification work through the
existing `ui.bg_ops` seam, while startup state inspection stays filesystem-only
and does not initialize the engine.

**Tech Stack:** Python 3.10 through 3.13, stdlib dataclasses/hashlib/shutil,
`huggingface_hub` supplied by `vieneu==3.3.0`, PySide6/QML, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-performance-optimization-implementation-design.md`

**Dependencies:** This is Phase 1. Phase 2 consumes `ModelManager` readiness
and its `ManagedModelLocation`; no later plan may add a second downloader or a
second model-cache format.

## Global Constraints

- Keep exactly one VieNeu engine instance and one inference worker.
- Keep model graph files out of the PyInstaller application artifact.
- Install only the official CPU ONNX-int8 baseline through this flow; custom
  model repositories remain an explicit advanced path and are never fetched
  automatically.
- Pin the installed backbone revision to
  `2da0efab622a1722125991736524f080b751ef5b` and codec revision to
  `ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae`.
- Download into `<data_dir>/models/official-v1/.staging/`, never the active
  directory. An interrupted or invalid staging directory is never usable.
- `TTSEngine` must pass local `backbone_repo`, `onnx_dir`, and `codec_dir`
  paths to the SDK for a managed install. It must not depend on `HF_HOME`,
  mutable Hub refs, or a caller’s shared Hugging Face cache.
- `ModelManager` imports `huggingface_hub` only inside a download operation.
- Use the repository’s injectable `bg_runner` and test fakes. Unit tests never
  contact Hugging Face or use the real user data directory.
- Do not log model paths from user profiles, tokens, request headers, user
  text, voice samples, or generated audio.
- Run timing-sensitive benchmark tests with `-n 0`.
- Each task uses TDD, focused tests, then the complete quality gate before its
  commit. Commit commands require explicit user authorization when executed.
- Complete quality gate:
  `./.venv/bin/ruff check .`,
  `./.venv/bin/ruff format --check .`, and
  `QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest`.

## File Map

### New production files

- `src/vienetts_app/core/official_model_manifest.py`
  - Immutable model format version, repository revisions, allowlisted files,
    expected byte sizes, SHA-256 values, and installation-size helpers.
- `src/vienetts_app/core/model_manager.py`
  - Filesystem inspection, staging download, checksum verification, atomic
    promotion, offline-pack validation, and typed state snapshots.

### Modified production files

- `src/vienetts_app/core/models.py`
  - Adds persisted model-cache preferences only after they have validated
    bounds.
- `src/vienetts_app/core/settings.py`
  - Persists the extended `Settings` shape through its existing atomic write.
- `src/vienetts_app/core/engine.py`
  - Accepts a validated managed install location and configures the local SDK
    paths without changing custom-repository behavior.
- `src/vienetts_app/ui/controller.py`
  - Exposes truthful model state and download controls, injects
    `ModelManager`, and builds an engine from an installed official baseline.
- `src/vienetts_app/app.py`
  - Starts the non-blocking model-state inspection after the QML shell loads.
- `src/vienetts_app/ui/qml/Main.qml`
  - Replaces developer-only missing-model guidance with setup/download,
    validating, retry, cancel, and offline guidance.
- `src/vienetts_app/ui/qml/SettingsTab.qml`
  - Identifies the managed official baseline versus a custom advanced source.
- `scripts/fetch_models.py`
  - Shares the committed official manifest and validates the same local
    install layout used by the app.
- `scripts/benchmarks/run_startup.py`
  - Measures one fresh child process per observation with the parent owning
    the process-start timestamp.
- `tests/smoke/test_ui_shell.py`
  - Replaces the command-overlay assertion with the model setup state machine.

### New tests

- `tests/unit/test_official_model_manifest.py`
- `tests/unit/test_model_manager.py`
- `tests/unit/test_startup_benchmark.py`

### Modified tests

- `tests/unit/test_models.py`
- `tests/unit/test_settings.py`
- `tests/unit/test_engine.py`
- `tests/unit/test_controller.py`
- `tests/unit/test_fetch_models.py`
- `tests/unit/test_app_entry.py`

---

### Task 1: Commit the official model format contract

**Files:**

- Create: `src/vienetts_app/core/official_model_manifest.py`
- Create: `tests/unit/test_official_model_manifest.py`
- Modify: `scripts/fetch_models.py`
- Modify: `tests/unit/test_fetch_models.py`

**Interfaces:**

- Produces:
  - `OFFICIAL_MODEL_FORMAT = "official-v1"`
  - `ModelFile(repo_key: Literal["backbone", "codec"], relative_path: str,
    size_bytes: int, sha256: str)`
  - `OfficialModelManifest(format_version: str, backbone_repo: str,
    backbone_revision: str, codec_repo: str, codec_revision: str,
    files: tuple[ModelFile, ...])`
  - `OFFICIAL_MODEL_MANIFEST`
  - `OfficialModelManifest.total_bytes: int`
  - `OfficialModelManifest.required_free_bytes: int`
  - `OfficialModelManifest.files_for(repo_key: str) -> tuple[ModelFile, ...]`
- Consumes: no Qt classes and no Hugging Face import.

- [ ] **Step 1: Write failing manifest tests**

```python
from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST


def test_official_manifest_is_immutable_and_complete() -> None:
    manifest = OFFICIAL_MODEL_MANIFEST
    assert manifest.format_version == "official-v1"
    assert manifest.backbone_revision == "2da0efab622a1722125991736524f080b751ef5b"
    assert manifest.codec_revision == "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae"
    assert {item.relative_path for item in manifest.files_for("backbone")} == {
        "config.json",
        "denoiser.onnx",
        "speaker_encoder.onnx",
        "onnx_int8/config.json",
        "onnx_int8/tokenizer.json",
        "onnx_int8/vieneu_acoustic_cached.onnx",
        "onnx_int8/vieneu_backbone_shared.data",
        "onnx_int8/vieneu_prefill.onnx",
        "onnx_int8/vieneu_decode_step.onnx",
        "onnx_int8/vieneu_v3_heads.npz",
    }
    assert len(manifest.files_for("codec")) == 6
    assert manifest.total_bytes == 327_034_699
    assert manifest.total_bytes == sum(item.size_bytes for item in manifest.files)
    assert manifest.required_free_bytes > manifest.total_bytes
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_official_model_manifest.py -v`

Expected: FAIL because `official_model_manifest` does not exist.

- [ ] **Step 3: Implement the exact immutable manifest**

Define all sixteen files with their repository-relative path, byte size, and
checksum. Construct the manifest with
`backbone_repo="pnnbao-ump/VieNeu-TTS-v3-Turbo"` and
`codec_repo="OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"` (alongside the
revisions already pinned in the test). The backbone file values are:

```python
ModelFile("backbone", "config.json", 1553,
          "eee8e032cb936a60312f594a8156c086173a9c0255a545bd11a448f22a7c77ae")
ModelFile("backbone", "denoiser.onnx", 42661414,
          "b7621953291cfe05e695a9c0ff4255aa2f93239fc17c26627e18b7b6b8f72f0b")
ModelFile("backbone", "speaker_encoder.onnx", 28303423,
          "a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73")
ModelFile("backbone", "onnx_int8/vieneu_backbone_shared.data", 103891968,
          "429bfddd585b7a1907c7c9c944b3d91bc4da8b91f1f9982353351357140fd08f")
```

Define the other six backbone entries exactly:

```python
ModelFile("backbone", "onnx_int8/config.json", 2152,
          "a9f8d9c4b4736448ab355d1a98cfe48f5e39aecf2916c37b0806c228612e9a2d")
ModelFile("backbone", "onnx_int8/tokenizer.json", 22320,
          "6cc6bcbe380b8c37bd9f2514e37c5dfa3e00e122c6e3125dae5c4afe48e39158")
ModelFile("backbone", "onnx_int8/vieneu_acoustic_cached.onnx", 7207223,
          "8f2d7306a35c6128793838f39c4c2da2c176e243bd63f0963c56bbf0376c3939")
ModelFile("backbone", "onnx_int8/vieneu_prefill.onnx", 1090823,
          "bc45488bd7802cd0e5d65cc427e9124e1a15b8b7e9fd86d37a3d16f1e847de4d")
ModelFile("backbone", "onnx_int8/vieneu_decode_step.onnx", 1062040,
          "8346ce8fefa3635a2dcd29b6f8a5cb23c7acfd5da9dfad54090b0f9b797c4b5a")
ModelFile("backbone", "onnx_int8/vieneu_v3_heads.npz", 52219622,
          "19ee6dd56530d7842c81fbd855f3d89440e2c3121e11f7e6ced447a559da585a")
```

Then define all six codec entries exactly:

```python
ModelFile("codec", "codec_browser_onnx_meta.json", 17036,
          "3e291c883bb7d11ff2fe8e964e3e495519760358859f35c951254c7741592731")
ModelFile("codec", "moss_audio_tokenizer_decode_full.onnx", 681902,
          "0fbbafe3fd4afa2a019af5c5ced204af6e2d1db044fa40f021525d2aee95b4ac")
ModelFile("codec", "moss_audio_tokenizer_decode_shared.data", 44198912,
          "e69d52e0f4e84ca27850557ee54face46632d3a5a16c89bd246c7c408466dcad")
ModelFile("codec", "moss_audio_tokenizer_decode_step.onnx", 351400,
          "9527c86a29e1837edec1f74db57d5eeaadb3a715af3382703566460afed25855")
ModelFile("codec", "moss_audio_tokenizer_encode.data", 44507136,
          "aa751265b2bab2887eac224484546b194875aa7494b607115439b3dc6b228a2c")
ModelFile("codec", "moss_audio_tokenizer_encode.onnx", 815775,
          "eadea4a645abdcf98714c7aead122ee2ce7da6e080f9f80b977cd1ca8e19473a")
```

DOWNLOAD_HEADROOM_BYTES = 512 * 1024 * 1024

@property
def required_free_bytes(self) -> int:
    return self.total_bytes + DOWNLOAD_HEADROOM_BYTES
```

Update `scripts/fetch_models.py` to import this manifest, pass each file’s
pinned `revision=` to `snapshot_download`, and write the shared
format-version/revision metadata. The developer CLI continues to support a
custom backbone only when explicitly requested, but its custom manifest must
identify that it is not the official managed format.

- [ ] **Step 4: Run focused manifest and fetch-script tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_official_model_manifest.py tests/unit/test_fetch_models.py -v`

Expected: PASS, with the fake downloader seeing the fixed revisions and
allowlisted paths.

- [ ] **Step 5: Commit the manifest contract**

```bash
git add src/vienetts_app/core/official_model_manifest.py \
  scripts/fetch_models.py \
  tests/unit/test_official_model_manifest.py \
  tests/unit/test_fetch_models.py
git commit -m "feat(models): pin official baseline manifest"
git notes add -m "Phase 1 Task 1: committed official model format and fetch contract."
```

### Task 2: Add a staging-only model manager

**Files:**

- Create: `src/vienetts_app/core/model_manager.py`
- Create: `tests/unit/test_model_manager.py`

**Interfaces:**

- Consumes:
  - `OFFICIAL_MODEL_MANIFEST: OfficialModelManifest`
  - `ModelFile`
- Produces:
  - `ModelInstallState = Literal["unavailable", "downloading", "validating",
    "ready", "failed", "custom"]`
  - `ManagedModelLocation(root: Path, backbone_dir: Path, onnx_dir: Path,
    codec_dir: Path, format_version: str, revision: str)`
  - `ModelStatus(state: ModelInstallState, installed_bytes: int,
    required_bytes: int, progress: float, error: str,
    location: ManagedModelLocation | None)`
  - `ModelManager(root: Path, manifest: OfficialModelManifest =
    OFFICIAL_MODEL_MANIFEST, downloader: Callable[..., Path] | None = None,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage)`
  - `inspect() -> ModelStatus`
  - `install(cancelled: Callable[[], bool] = lambda: False,
    on_progress: Callable[[ModelStatus], None] = lambda _status: None) ->
    ModelStatus`
  - `install_offline_pack(source: Path) -> ModelStatus`
  - `cancel_staging() -> None`

- [ ] **Step 1: Write failing state, validation, and recovery tests**

```python
from pathlib import Path

from vienetts_app.core.model_manager import ModelManager


def test_incomplete_staging_is_not_reported_as_ready(tmp_path: Path) -> None:
    manager = ModelManager(tmp_path / "models", manifest=mini_manifest())
    staging = manager.root / ".staging" / "official-v1"
    (staging / "backbone").mkdir(parents=True)
    (staging / "backbone" / "config.json").write_bytes(b"wrong")

    status = manager.inspect()

    assert status.state == "unavailable"
    assert status.location is None


def test_validated_staging_is_promoted_atomically(tmp_path: Path) -> None:
    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=materialize_requested_file,
    )

    status = manager.install()

    assert status.state == "ready"
    assert status.location is not None
    assert (status.location.root / "install.json").is_file()
    assert not (manager.root / ".staging" / "official-v1").exists()


def test_checksum_failure_never_creates_an_active_install(tmp_path: Path) -> None:
    manager = ModelManager(
        tmp_path / "models",
        manifest=mini_manifest(),
        downloader=lambda **_kwargs: write_bytes(b"corrupt"),
    )

    status = manager.install()

    assert status.state == "failed"
    assert status.location is None
    assert not (manager.root / "official-v1" / "install.json").exists()
```

Complete the same test file with these assertions:

- a disk-usage fake below `manifest.required_free_bytes` returns `failed`,
  calls no downloader, and leaves no active install;
- cancellation after the first verified file returns `unavailable`, calls no
  downloader for the second file, and preserves only verified staging files;
- a valid first staging file causes install to download only the second file;
- an offline pack containing any relative path outside `manifest.files`
  returns `failed` and never creates `install.json`;
- `inspect()` reports the filesystem state while a fake downloader that raises
  `AssertionError("download invoked")` remains untouched.

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_model_manager.py -v`

Expected: FAIL because `ModelManager` does not exist.

- [ ] **Step 3: Implement inspection, download, validation, and promotion**

Use this installation layout:

```text
<data_dir>/models/
  official-v1/
    install.json
    backbone/
      config.json
      denoiser.onnx
      speaker_encoder.onnx
      onnx_int8/...
    codec/
      codec_browser_onnx_meta.json
      moss_audio_tokenizer_*.onnx
      moss_audio_tokenizer_*.data
  .staging/
    official-v1/
      backbone/
      codec/
```

`install()` must lazily import `hf_hub_download` and call it once per
allowlisted file with:

```python
downloaded = hf_hub_download(
    repo_id=repo_id,
    filename=model_file.relative_path,
    revision=revision,
    local_dir=str(staging_repo_dir),
    local_dir_use_symlinks=False,
)
```

After every download, verify the returned/target file’s size and stream its
SHA-256 in 1 MiB blocks. Emit a status with
`progress = verified_files / len(manifest.files)`. Before starting a file and
before promotion, check `cancelled()`. On cancellation, leave the staging
directory intact only when every already-present file validates; otherwise
remove the invalid file. Never write `install.json` until all files validate.

Implement promotion as a same-volume `os.replace(staging, active)` after
moving any prior valid active install to `official-v1.previous`, then remove
the previous directory only after `inspect()` accepts the new install.
If promotion fails, restore the previous valid install. `install.json` stores
only format/repository/revision/file metadata and validation time, never a
token or user path.

`install_offline_pack(source)` validates the identical directory format in a
separate staging copy and calls the same promotion routine. It does not unpack
archives or invent a second cache representation.

- [ ] **Step 4: Run focused tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_model_manager.py -v`

Expected: PASS, with no network access and no files outside `tmp_path`.

- [ ] **Step 5: Commit the model manager**

```bash
git add src/vienetts_app/core/model_manager.py tests/unit/test_model_manager.py
git commit -m "feat(models): add validated managed installer"
git notes add -m "Phase 1 Task 2: added staging, verification, recovery, and offline-pack installation."
```

### Task 3: Wire managed installs into the engine without remote fallback

**Files:**

- Modify: `src/vienetts_app/core/engine.py`
- Modify: `src/vienetts_app/core/models.py`
- Modify: `src/vienetts_app/core/settings.py`
- Modify: `tests/unit/test_engine.py`
- Modify: `tests/unit/test_models.py`
- Modify: `tests/unit/test_settings.py`

**Interfaces:**

- Consumes:
  - `ManagedModelLocation`
  - `ModelManager.inspect().location`
- Produces:
  - `TTSEngine(..., managed_model: ManagedModelLocation | None = None)`
  - `Settings.model_cache_enabled: bool = True`
  - `TTSEngine` local factory kwargs:
    `backbone_repo=str(location.backbone_dir)`,
    `onnx_dir=str(location.onnx_dir)`, and
    `codec_dir=str(location.codec_dir)`.

- [ ] **Step 1: Write failing local-engine path tests**

```python
from pathlib import Path

from vienetts_app.core.engine import TTSEngine
from vienetts_app.core.model_manager import ManagedModelLocation


def test_managed_install_passes_only_local_sdk_paths(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    location = ManagedModelLocation(
        root=tmp_path,
        backbone_dir=tmp_path / "backbone",
        onnx_dir=tmp_path / "backbone" / "onnx_int8",
        codec_dir=tmp_path / "codec",
        format_version="official-v1",
        revision="2da0efab622a1722125991736524f080b751ef5b",
    )

    TTSEngine(factory=lambda **kwargs: observed.update(kwargs) or FakeVieneu(),
              managed_model=location).initialize()

    assert observed["backbone_repo"] == str(location.backbone_dir)
    assert observed["onnx_dir"] == str(location.onnx_dir)
    assert observed["codec_dir"] == str(location.codec_dir)
```

Complete the source-selection matrix with these assertions:

- empty official-source settings plus a ready managed location and selected
  `"auto"` construct with `backend="onnx"` and that local location;
- explicit `backend="torch"` constructs without `managed_model`;
- a nonempty custom `model_repo` constructs without `managed_model`, even
  when the official baseline is ready.

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_engine.py tests/unit/test_models.py tests/unit/test_settings.py -v`

Expected: FAIL because `TTSEngine` has no `managed_model` argument.

- [ ] **Step 3: Implement model-source selection**

Add `managed_model` as an optional immutable engine construction input. When
present, add the three local path kwargs shown above and preserve `precision`
and profile-selected `threads`. Do not set `HF_HOME` or alter process-global
Hub environment variables.

At the controller call site, resolve settings as follows:

```python
managed = self._model_status.location
using_official = self._settings.model_repo == "" and managed is not None
backend = "onnx" if using_official and self._settings.backend == "auto" else self._settings.backend
engine = self._engine_factory(
    backend=backend,
    precision=self._settings.precision,
    voices_dir=self._voices_dir,
    model_repo=self._settings.model_repo,
    managed_model=managed if using_official else None,
)
```

This guarantees that the downloaded CPU baseline works on a clean
CUDA-capable machine without accidentally entering the uninstalled Torch
path. An explicit Torch selection remains actionable and does not claim that
the CPU baseline supplies Torch weights.

- [ ] **Step 4: Run focused tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_engine.py tests/unit/test_models.py tests/unit/test_settings.py -v`

Expected: PASS, including legacy engine construction without a managed model.

- [ ] **Step 5: Commit managed engine wiring**

```bash
git add src/vienetts_app/core/engine.py src/vienetts_app/core/models.py \
  src/vienetts_app/core/settings.py tests/unit/test_engine.py \
  tests/unit/test_models.py tests/unit/test_settings.py
git commit -m "feat(engine): use verified local model installs"
git notes add -m "Phase 1 Task 3: wired official model installs to local SDK paths."
```

### Task 4: Expose model setup and truthful readiness in the application

**Files:**

- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/app.py`
- Modify: `src/vienetts_app/ui/qml/Main.qml`
- Modify: `src/vienetts_app/ui/qml/SettingsTab.qml`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/smoke/test_ui_shell.py`

**Interfaces:**

- Consumes:
  - `ModelManager.inspect() -> ModelStatus`
  - `ModelManager.install(cancelled, on_progress) -> ModelStatus`
- Produces QML properties and slots:
  - `modelState: str`
  - `modelProgress: float`
  - `modelError: str`
  - `modelInstalledBytes: int`
  - `modelRequiredBytes: int`
  - `modelReady: bool`
  - `refreshModelState() @Slot()`
  - `downloadOfficialModel() @Slot()`
  - `cancelModelDownload() @Slot()`
  - `modelStateChanged`, `modelProgressChanged`, `modelErrorChanged`,
    `modelStorageChanged`

- [ ] **Step 1: Write failing controller and shell tests**

```python
def test_download_model_updates_state_without_initializing_engine(harness) -> None:
    harness.model_manager.queue_statuses(
        unavailable_status(),
        downloading_status(progress=0.5),
        ready_status(),
    )

    harness.controller.downloadOfficialModel()

    assert harness.controller.modelState == "ready"
    assert harness.controller.modelProgress == 1.0
    assert harness.engines == []


def test_retry_refreshes_state_instead_of_dismissing_it(harness) -> None:
    harness.model_manager.status = unavailable_status(error="Network unavailable")

    harness.controller.refreshModelState()

    assert harness.model_manager.inspect_calls == 1
    assert harness.controller.modelState == "unavailable"
```

Extend the shell smoke driver with fake model-manager states. Assert that the
initial card says checking/unavailable rather than ready, the setup card
contains model size and storage requirement, retry invokes
`refreshModelState()`, and cancel invokes `cancelModelDownload()` without a
developer command string.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_controller.py tests/smoke/test_ui_shell.py -v`

Expected: FAIL because model-state properties and slots do not exist.

- [ ] **Step 3: Implement model state ownership and non-blocking setup**

Inject a `model_manager_factory` into `AppController`, defaulting to:

```python
def _default_model_manager(data_dir: Path) -> ModelManager:
    return ModelManager(data_dir / "models")
```

`AppController.__init__` constructs it but does not scan files or import Hub
code. `refreshModelState()` submits `manager.inspect` through `_run_bg`; it
checks that the result belongs to the same controller before publishing it.
`downloadOfficialModel()` submits `manager.install` through the same seam,
passes a controller-owned `threading.Event` cancellation predicate, and
marshals incremental `ModelStatus` values through a Qt signal. It refuses a
download while a custom model repo is selected and surfaces an explicit
advanced-setting explanation.

In `run_gui()`, schedule `controller.refreshModelState` after first paint,
alongside the existing deferred hardware note. Do not call it in `create_app`.

Replace the `modelsMissingOverlay` command panel with a setup panel that:

1. distinguishes checking, unavailable, downloading, validating, ready, and
   failed;
2. displays `modelInstalledBytes / modelRequiredBytes` using a local
   human-size formatter;
3. offers Download, Cancel, Retry, and offline-pack guidance;
4. leaves synthesis controls disabled until `modelReady` or an explicit
   custom-source flow is selected;
5. has stable object names:
   `modelSetupOverlay`, `modelDownloadButton`, `modelCancelButton`,
   `modelRetryButton`, `modelProgressBar`, and `modelStatusText`.

Use the existing `AppButton` tooltip behavior and Vietnamese `qsTr` copy. The
engine status card uses model state to choose neutral/warning/success instead
of hardcoding “Sẵn sàng”.

- [ ] **Step 4: Run controller and QML smoke tests**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_controller.py tests/smoke/test_ui_shell.py -v`

Expected: PASS, including a clean-profile setup state and a real retry refresh.

- [ ] **Step 5: Commit truthful onboarding UI**

```bash
git add src/vienetts_app/ui/controller.py src/vienetts_app/app.py \
  src/vienetts_app/ui/qml/Main.qml src/vienetts_app/ui/qml/SettingsTab.qml \
  tests/unit/test_controller.py tests/smoke/test_ui_shell.py
git commit -m "feat(ui): add in-app official model setup"
git notes add -m "Phase 1 Task 4: added truthful model readiness and in-app downloader controls."
```

### Task 5: Make startup measurement process-cold and reproducible

**Files:**

- Modify: `scripts/benchmarks/run_startup.py`
- Create: `tests/unit/test_startup_benchmark.py`
- Modify: `scripts/benchmarks/schema.py`
- Modify: `scripts/benchmarks/summarize.py`
- Modify: `tests/unit/test_benchmark_schema.py`
- Modify: `tests/unit/test_benchmark_statistics.py`
- Modify: `docs/performance/README.md`

**Interfaces:**

- Produces:
  - `run_startup.py --iterations N` launching `N` fresh child processes.
  - Child-only `--child-output <path>` mode writing one JSON object to stdout.
  - Startup tags: `process_start_parent_ns`, `qml_loaded`, `window_exposed`,
    `first_frame_swapped`, `frame_signal_supported`.
  - Summary labels `process_cold_startup_ms` and
    `in_process_qml_boot_ms`, never a misleading repeated-process “startup”.

- [ ] **Step 1: Write a failing subprocess-orchestration test**

```python
from scripts.benchmarks import run_startup


def test_parent_launches_a_fresh_child_per_iteration(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[command.index("--child-output") + 1]).write_text(
            '{"frame_signal_supported": false, "events": []}\n', encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_startup.subprocess, "run", fake_run)
    assert run_startup.run(run_startup._parser().parse_args(
        ["--iterations", "3", "--output", str(tmp_path / "out.jsonl")]
    )) == 0
    assert len(commands) == 3
    assert all("--child-output" in command for command in commands)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_startup_benchmark.py -v`

Expected: FAIL because the runner currently calls `_run_one` repeatedly in its
own process.

- [ ] **Step 3: Implement parent-owned timing and child-owned QML probing**

Move the current QML construction routine into a child entrypoint. The parent
records `time.perf_counter_ns()` immediately before `subprocess.run`, passes
only command-line-safe capability flags and a temporary output path, and
computes `process_cold_startup_ms` from the child’s returned first-frame or
exposure milestone. The child records its own import/QML milestones separately.

The parent uses:

```python
subprocess.run(
    [sys.executable, "-m", "scripts.benchmarks.run_startup", "--child-output", str(path)],
    cwd=Path.cwd(),
    env={**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen")},
    capture_output=True,
    text=True,
    check=False,
)
```

Do not reuse a `QGuiApplication`, process, temporary model state, or model
manager between iterations. Preserve the current content-safe benchmark schema
and mark frame-swap absence as unsupported rather than inventing a frame time.

- [ ] **Step 4: Run focused benchmark tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_startup_benchmark.py tests/unit/test_benchmark_schema.py tests/unit/test_benchmark_statistics.py -v`

Expected: PASS, with every record labeled by its cold-process method.

- [ ] **Step 5: Update benchmark documentation**

Document the precise meanings of process-cold, page-cache-warm, model-warm,
direct-engine, and production-pipeline numbers. State that the model downloader
is excluded from startup metrics and that no benchmark record may contain
personal file paths or source text.

- [ ] **Step 6: Commit benchmark correction**

```bash
git add scripts/benchmarks/run_startup.py scripts/benchmarks/schema.py \
  scripts/benchmarks/summarize.py tests/unit/test_startup_benchmark.py \
  tests/unit/test_benchmark_schema.py tests/unit/test_benchmark_statistics.py \
  docs/performance/README.md
git commit -m "fix(perf): measure startup in fresh processes"
git notes add -m "Phase 1 Task 5: corrected startup benchmark process isolation."
```

### Task 6: Run Phase 1 integration verification

**Files:**

- Modify: `README.md`
- Modify: `PROJECT_PLAN.md`
- Modify: `conductor/patterns.md` only if the implementation discovers a
  reusable project-wide convention.

**Interfaces:**

- Consumes: all Phase 1 public interfaces.
- Produces: truthful model-install documentation that says the application is
  offline after one-time model setup, not immediately after application
  download.

- [ ] **Step 1: Add an offline local-path engine test**

```python
def test_ready_managed_install_initializes_without_hub_access(tmp_path, monkeypatch) -> None:
    location = make_valid_managed_install(tmp_path)
    monkeypatch.setattr("vienetts_app.core.engine._default_factory", fake_local_only_factory)

    engine = TTSEngine(backend="onnx", managed_model=location)
    engine.initialize()

    assert fake_local_only_factory.received_local_paths is True
```

- [ ] **Step 2: Run the Phase 1 focused suite**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_official_model_manifest.py \
  tests/unit/test_model_manager.py \
  tests/unit/test_engine.py \
  tests/unit/test_controller.py \
  tests/unit/test_startup_benchmark.py \
  tests/smoke/test_ui_shell.py -v
```

Expected: PASS. No test may make a live Hub request.

- [ ] **Step 3: Update user-facing documentation**

Replace instructions that tell packaged users to run
`python scripts/fetch_models.py`. Describe the in-app official CPU model
setup, its approximate download size, free-space preflight, cancellation,
offline operation after a valid install, advanced custom-source boundary, and
the developer-only `scripts/fetch_models.py` workflow.

- [ ] **Step 4: Run the complete quality gate**

Run:

```bash
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit Phase 1 documentation and verification**

```bash
git add README.md PROJECT_PLAN.md conductor/patterns.md \
  src/vienetts_app/core/engine.py tests/unit/test_engine.py
git commit -m "docs(models): describe one-time offline setup"
git notes add -m "Phase 1 Task 6: verified clean-profile setup and documented offline-after-setup behavior."
```
