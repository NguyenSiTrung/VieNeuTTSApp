# Windows Audio Export Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Windows Save WAV dialog initial folder URL defect and make all user-facing audio exports broadly compatible (standard 16-bit PCM) and atomic with Windows lock resilience.

**Architecture:**
1. Add `path_to_file_url` in `core/paths.py` and expose `outputDirUrl` / `pathToUrl` on `AppController`, coupled with a safe `toFolderUrl` helper in QML so `FileDialog.currentFolder` is always a valid `QUrl` (e.g. `file:///C:/Users/...` instead of `file://C:\Users\...`).
2. Add `export_wav_file` in `core/audio.py` that stream-copies audio in 64k frame blocks from the internal float artifact to a standard 16-bit PCM WAV (WAVE format tag 0x0001) in a same-directory `.part.wav`, validates it via `validate_wav_artifact`, and atomically promotes it with `os.replace` + bounded retry for Windows locks.
3. Wire `export_wav_file` into `controller.exportWav`, `AudiobookLibrary.export_chapter`, and `batch_controller`, keeping internal artifacts as float while ensuring user exports open without "unsupported/raw format" issues in Windows tools.

**Tech Stack:** Python 3.13, PySide6 6.11.2 (QML QtQuick.Dialogs), soundfile, numpy.

**Spec / Issues:** Beads `VieNeuTTSApp-2td` and `VieNeuTTSApp-y47`.

## Global Constraints

- Keep the internal synthesis and interactive replay artifacts as 48 kHz 32-bit float (`subtype="FLOAT"`).
- User-facing exported WAV files must be standard mono 48 kHz 16-bit PCM (`subtype="PCM_16"`, format tag `0x0001`).
- Export must use bounded block streaming (negligible RAM, never allocating whole-audio arrays).
- Writes must be atomic: write to `<destination>.part.wav`, validate, promote via `os.replace`. On error, temp file is removed and source remains untouched.
- Preserve Windows file lock resilience: retry loop on `PermissionError`.
- `ruff check .`, `ruff format --check .`, and `pytest` must pass.

---

### Task 1: Fix Windows Export Dialog Initial Folder URL (`VieNeuTTSApp-2td`)

**Files:**
- Modify: `src/vienetts_app/core/paths.py`
- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/ui/qml/TextTab.qml`
- Modify: `src/vienetts_app/ui/qml/SettingsTab.qml`
- Test: `tests/unit/test_paths.py`
- Test: `tests/unit/test_controller.py`

**Interfaces:**
- Consumes: `path_or_url: str | Path | None`
- Produces: `path_to_file_url(path: str | Path | None) -> str`
- Exposes: `@Property(str, notify=outputDirChanged) def outputDirUrl(self) -> str`
- Exposes: `@Slot(str, result=str) def pathToUrl(self, path: str) -> str`

- [ ] **Step 1: Write failing tests for `path_to_file_url` and `controller.outputDirUrl`**

Add tests to `tests/unit/test_paths.py`:
- Windows path with backslashes `C:\Users\Alice\Music` -> `file:///C:/Users/Alice/Music`
- Windows path with forward slashes `C:/Users/Alice/Music` -> `file:///C:/Users/Alice/Music`
- Path with Vietnamese characters and spaces `C:\Users\Trung\Nhạc Việt` -> `file:///C:/Users/Trung/Nh%E1%BA%A1c%20Vi%E1%BB%87t` or valid QUrl
- UNC path `\\server\share\music` -> `file://server/share/music`
- Unix path `/home/user/Music` -> `file:///home/user/Music`
- Empty / whitespace / None -> `""`
- Already `file://` URL -> preserved / normalized

Add test to `tests/unit/test_controller.py`:
- `controller.outputDirUrl` returns valid file URL for `controller.outputDir` and default Music dir.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_paths.py::TestPathToFileUrl tests/unit/test_controller.py::TestOutputDirUrl`
Expected: FAIL (ImportError / AttributeError)

- [ ] **Step 3: Implement `path_to_file_url` in `core/paths.py` and wire into `controller.py`**

In `src/vienetts_app/core/paths.py`:
```python
def path_to_file_url(path_or_url: str | Path | None) -> str:
    """Convert any local file or directory path into a valid file:// URL string.

    Handles:
    - None or empty string -> ""
    - Windows backslash paths (C:\\... -> file:///C:/...)
    - Windows forward slash paths (C:/... -> file:///C:/...)
    - UNC network shares (\\\\server\\share -> file://server/share)
    - Unix paths (/home/... -> file:///home/...)
    - URL percent-encoding for spaces and non-ASCII characters
    - Already valid file:// URLs
    """
    if path_or_url is None:
        return ""
    raw = str(path_or_url).strip()
    if not raw:
        return ""
    # Strip quotes
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
        if not raw:
            return ""
    if raw.startswith("file://"):
        # Ensure it has 3 slashes if followed by drive letter
        after_scheme = raw[7:]
        if re.match(r"^[A-Za-z]:[/\\]", after_scheme):
            return f"file:///{after_scheme.replace('\\', '/')}"
        return raw
    # Normalize backslashes
    clean = raw.replace("\\", "/")
    # UNC share: //server/share
    if clean.startswith("//"):
        server_and_path = clean[2:]
        return f"file://{server_and_path}"
    # Windows drive: C:/path
    if re.match(r"^[A-Za-z]:/", clean):
        return f"file:///{clean}"
    if clean.startswith("/"):
        return f"file://{clean}"
    return f"file:///{clean}"
```

In `src/vienetts_app/ui/controller.py`:
```python
    @Property(str, notify=outputDirChanged)
    def outputDirUrl(self) -> str:
        """file:// URL for the current or default output directory."""
        dir_path = self._settings.output_dir.strip()
        if not dir_path:
            dir_path = str(self._default_export_path().parent)
        return path_to_file_url(dir_path)

    @Slot(str, result=str)
    def pathToUrl(self, path: str) -> str:
        return path_to_file_url(path)
```

In `src/vienetts_app/ui/qml/TextTab.qml`:
Add `toFolderUrl(path)` helper and update `openExportDialog`:
```qml
    function toFolderUrl(path) {
        if (!path || path.trim() === "")
            return "";
        if (typeof controller.pathToUrl === "function") {
            const u = controller.pathToUrl(path);
            if (u !== "")
                return u;
        }
        if (path.startsWith("file://"))
            return path;
        const clean = path.replace(/\\/g, "/");
        if (/^[A-Za-z]:\//.test(clean))
            return "file:///" + clean;
        if (clean.startsWith("//"))
            return "file:" + clean;
        if (clean.startsWith("/"))
            return "file://" + clean;
        return "file:///" + clean;
    }

    function openExportDialog() {
        const folder = controller.outputDir !== ""
            ? root.toFolderUrl(controller.outputDir)
            : (controller.outputDirUrl || "");
        if (folder !== "")
            exportDialog.currentFolder = folder;
        exportDialog.open();
    }
```

In `src/vienetts_app/ui/qml/SettingsTab.qml`:
Update `outputDirBrowseButton` to set `outputDirDialog.currentFolder` before opening:
```qml
onClicked: {
    if (controller.outputDir !== "") {
        const folder = root.toFolderUrl(controller.outputDir);
        if (folder !== "")
            outputDirDialog.currentFolder = folder;
    }
    outputDirDialog.open();
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_paths.py tests/unit/test_controller.py`
Expected: PASS

---

### Task 2: Implement Streaming Atomic WAV Export Helper (`VieNeuTTSApp-y47` Part 1)

**Files:**
- Modify: `src/vienetts_app/core/audio.py`
- Test: `tests/unit/test_audio.py`

**Interfaces:**
- Consumes: `source: str | Path`, `destination: str | Path`, `subtype: str = "PCM_16"`, `block_frames: int = 65_536`
- Produces: `export_wav_file(source: str | Path, destination: str | Path, *, subtype: str = "PCM_16", block_frames: int = 65_536) -> Path`

- [ ] **Step 1: Write failing tests for `export_wav_file` in `tests/unit/test_audio.py`**

Tests:
1. `test_export_wav_converts_float_to_pcm16`: writes float WAV, calls `export_wav_file`, verifies output format is `WAV (Microsoft)`, subtype is `PCM_16`, format tag is `1` (`WAVE_FORMAT_PCM`), sample rate is 48 kHz, frame count matches.
2. `test_export_wav_audio_content_matches`: reads audio via `read_wav`, verifies signal shape matches source within float-to-16bit quantization tolerance (`np.allclose(data, src_data, atol=1e-3)`).
3. `test_export_wav_clamps_float_overshoots`: float samples slightly outside `[-1.0, 1.0]` do not wrap around or error.
4. `test_export_wav_is_atomic_on_failure`: injects error midway; verifies partial `.part.wav` is cleaned up and original destination (if any) is preserved.
5. `test_export_wav_retries_on_transient_permission_error`: verifies Windows lock retry loop succeeds when transient lock clears.
6. `test_export_wav_nonexistent_source_raises`: raises `FileNotFoundError`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_audio.py::TestExportWavFile`
Expected: FAIL with `ImportError: cannot import name 'export_wav_file'`

- [ ] **Step 3: Implement `export_wav_file` in `core/audio.py`**

In `src/vienetts_app/core/audio.py`:
```python
def export_wav_file(
    source: str | Path,
    destination: str | Path,
    *,
    subtype: str = "PCM_16",
    block_frames: int = 65_536,
) -> Path:
    """Stream-convert a WAV artifact to a standard PCM WAV file atomically.

    Reads from ``source`` in fixed-size blocks (default 64k frames, negligible
    memory) to avoid loading long documents or audiobooks into RAM.
    Converts to standard 16-bit PCM (WAVE format tag 0x0001) with sample clamping.
    Writes to a temporary `.part.wav` in ``destination.parent``, validates
    integrity, and atomically promotes to ``destination`` via ``os.replace``
    with retry for Windows file locks.

    If conversion or promotion fails, the temporary part file is unlinked
    and the source file is left untouched.
    """
    import contextlib
    import os
    import time
    import uuid

    src_path = Path(source)
    if not src_path.is_file():
        raise FileNotFoundError(f"Source audio file does not exist: {src_path}")

    dest_path = Path(destination)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    part_path = dest_path.parent / f".{dest_path.stem}.{uuid.uuid4().hex}.part.wav"

    sf = _sf()
    try:
        with sf.SoundFile(str(src_path), mode="r") as reader:
            sr = reader.samplerate
            channels = reader.channels
            with sf.SoundFile(
                str(part_path),
                mode="w",
                samplerate=sr,
                channels=channels,
                subtype=subtype,
                format="WAV",
            ) as writer:
                while True:
                    block = reader.read(block_frames, dtype="float32", always_2d=False)
                    if len(block) == 0:
                        break
                    np.clip(block, -1.0, 1.0, out=block)
                    writer.write(block)

        # Validate written file
        from vienetts_app.core.artifacts import validate_wav_artifact

        validate_wav_artifact(part_path)

        # Atomic promote with bounded retry for Windows locks
        for attempt in range(5):
            try:
                os.replace(part_path, dest_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2**attempt))
    except Exception:
        with contextlib.suppress(OSError):
            part_path.unlink(missing_ok=True)
        raise

    return dest_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_audio.py::TestExportWavFile`
Expected: PASS

---

### Task 3: Integrate `export_wav_file` Across Export Surfaces and Update Existing Tests (`VieNeuTTSApp-y47` Part 2)

**Files:**
- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/core/audiobook.py`
- Modify: `src/vienetts_app/ui/batch_controller.py`
- Modify: `src/vienetts_app/ui/qml/TextTab.qml`
- Modify: `tests/unit/test_controller.py`

- [ ] **Step 1: Replace raw copies with `export_wav_file` in controllers**

In `src/vienetts_app/ui/controller.py`:
In `exportWav(self, path: str)`:
```python
        from vienetts_app.core.audio import export_wav_file

        def work() -> tuple[str, str]:
            try:
                export_wav_file(source, target, subtype="PCM_16")
                return str(target), ""
            except PermissionError as exc:
                return "", self.tr("Tệp đang được sử dụng bởi ứng dụng khác: {}").format(exc)
            except OSError as exc:
                return "", self.tr("Xuất WAV thất bại: {}").format(exc)
            except Exception as exc:
                return "", self.tr("Xuất WAV thất bại: {}").format(exc)
```

In `src/vienetts_app/core/audiobook.py`:
In `export_chapter`:
```python
        from vienetts_app.core.audio import export_wav_file
        try:
            export_wav_file(source, target, subtype="PCM_16")
        except PermissionError as exc:
            raise AudiobookError(
                f"Could not export the chapter (file locked by another program): {exc}"
            ) from exc
        except OSError as exc:
            raise AudiobookError(f"Could not export the chapter: {exc}") from exc
        except Exception as exc:
            raise AudiobookError(f"Could not export the chapter: {exc}") from exc
        return target
```

In `src/vienetts_app/ui/batch_controller.py`:
In `_start_save`:
```python
        from vienetts_app.core.audio import export_wav_file

        def work() -> tuple[int, str, str]:
            try:
                export_wav_file(source, target, subtype="PCM_16")
                return (uid, str(target), "")
            except OSError as exc:
                return (uid, "", str(exc))
            except Exception as exc:
                return (uid, "", str(exc))
```

In `src/vienetts_app/ui/qml/TextTab.qml`:
Update notice title for export errors:
```qml
        AppNotice {
            objectName: "textErrorNotice"
            Layout.fillWidth: true
            tone: "error"
            title: (controller.errorText.indexOf(qsTr("Xuất")) !== -1
                    || controller.errorText.indexOf(qsTr("xuất")) !== -1)
                ? qsTr("Không thể xuất tệp âm thanh")
                : qsTr("Không thể tạo âm thanh")
            message: controller.errorText
            messageObjectName: "errorLabel"
            visible: controller.errorText !== ""
        }
```

- [ ] **Step 2: Update existing controller tests asserting raw byte equality**

In `tests/unit/test_controller.py`:
In `test_completed_artifact_enables_copy_export_without_held_numpy_audio`:
Replace `assert target.read_bytes() == source.read_bytes()` with:
```python
        assert target.is_file()
        info = sf.info(str(target))
        assert info.subtype == "PCM_16"
        data, sr = read_wav(target)
        assert sr == 48_000
        src_data, _ = read_wav(source)
        assert np.allclose(data, src_data, atol=1e-3)
```

In `test_export_protects_retired_artifact_until_background_copy_releases`:
Replace `assert target.read_bytes() == first_bytes` with:
```python
        assert target.is_file()
        info = sf.info(str(target))
        assert info.subtype == "PCM_16"
```

- [ ] **Step 3: Run controller and audiobook tests**

Run: `.venv/bin/pytest -q tests/unit/test_controller.py tests/unit/test_audiobook.py tests/unit/test_batch_controller.py`
Expected: PASS

---

### Task 4: Full Validation Gate and Bead Resolution

- [ ] **Step 1: Run linter and formatting checks**

Run: `.venv/bin/ruff check .`
Run: `.venv/bin/ruff format --check .`

- [ ] **Step 2: Run unit test suite and smoke tests**

Run: `QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest -q tests/unit`
Run: `QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest -q tests/smoke/test_e2e_flows.py`

- [ ] **Step 3: Verify exported WAV with `scripts/check_smoke_wav.py`**

Run a smoke export and verify format with `scripts/check_smoke_wav.py` and `sf.info`.

- [ ] **Step 4: Close Beads issues**

Close `VieNeuTTSApp-2td` and `VieNeuTTSApp-y47` with verified test output.
