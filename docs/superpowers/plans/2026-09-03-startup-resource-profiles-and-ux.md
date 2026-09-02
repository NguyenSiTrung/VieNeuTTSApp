# Startup, Resource Profiles, and UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep first paint free of model/audio/hardware work, expose safe
Performance/Auto/Efficiency resource choices, and finish the specified
truthfulness, navigation, text-metric, and scrolling UX corrections.

**Architecture:** Startup publishes only passive initial state, then schedules
model inspection, audio probing, hardware detection, and optional warmup after
the first rendered frame. A pure resource-profile module maps the selected
profile plus broad hardware/physical-core evidence to safe ONNX thread values.
QML receives state rather than triggering side effects in property getters,
and shared `TextMetrics` debounces whole-text scans once per editing pause.

**Tech Stack:** Python stdlib/platform detection, PySide6/QML,
`QTimer`/`QQuickWindow`, pytest, QML smoke tests, benchmark scripts, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-performance-optimization-implementation-design.md`

**Dependencies:** Implement after
`2026-09-03-priority-scheduling-and-audiobook-cache.md`. Phase 1 supplies
model status and Phase 2 supplies foreground job state. This phase must not
change inference scheduling, artifact semantics, cache identity, model
installation format, or add raw thread-count controls to QML.

## Global Constraints

- The first frame precedes audio device enumeration, hardware probing, model
  initialization, CUDA checks, model downloads, and automatic warmup.
- `create_app()` remains passive and model-free. QML property reads must not
  synchronously enumerate audio devices or initialize a model.
- Model state is truthfully one of checking, unavailable, downloading,
  validating, ready, initializing, or failed. It never displays ready until
  verified.
- Audio state is explicitly `checking`, `available`, or `unavailable`; an
  unknown state must not show an incorrect export-only warning.
- Expose exactly `Auto`, `Performance`, and `Efficiency` profiles. Do not
  expose raw ONNX threads, CPU affinity, ORT session count, or microbatch
  controls in product UI.
- M4-class seed values are Performance=4, Auto=2, Efficiency=1 threads,
  clamped to available physical-core evidence. Other hardware uses safe
  clamped seed values and is measured before changing defaults.
- Profile selection takes effect on next engine initialization and follows
  existing `needsRestart` semantics if an engine already exists.
- Automatic warmup occurs only after a useful explicit intent, such as stable
  nonblank text or successful EPUB import, and never in Efficiency mode. It
  runs on the existing worker, never on the GUI thread.
- Local calibration is intentionally not shipped in this phase. It must not
  run silently, delay first paint, or affect a profile without user action and
  stored benchmark evidence.
- Compact icon-only navigation exposes a sighted-user tooltip and an
  accessible name for every destination.
- Whole-document word/duration metrics are debounced. At most one scan runs
  per 250 ms editing idle period; no repeated regular-expression scan is
  evaluated from multiple bindings per keystroke.
- Validate nested wheel/touch scroll handoff on the real rendered UI. Do not
  replace scroll behavior based solely on static inspection; if the fixed
  harness finds a regression, file a scoped Beads issue with the captured
  trace before changing interaction code.
- Benchmarks use fresh process and report machine/profile/measurement method
  without source text, voice samples, output audio, credentials, or personal
  paths.
- Before each commit, run the task’s focused tests. Before Phase completion,
  run:
  `./.venv/bin/ruff check .`,
  `./.venv/bin/ruff format --check .`, and
  `QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest`.

## File Map

### New production files

- `src/vienetts_app/core/resource_profiles.py`
  - Profile types, physical-core discovery seam, deterministic thread
    resolution, and resource policy documentation.
- `src/vienetts_app/ui/qml/components/TextMetrics.qml`
  - Shared debounced word/duration metric cache for text and paragraph tabs.

### New benchmark/test-support files

- `scripts/benchmarks/run_profiles.py`
  - Repeats direct-engine and production-pipeline profile measurements using
    the existing privacy-safe benchmark schema.
- `scripts/benchmarks/run_scroll_handoff.py`
  - Reproducible real-window wheel/touch validation harness with phase
    markers and threshold calculation.
- `tests/unit/test_resource_profiles.py`
- `tests/unit/test_startup_deferred_work.py`

### Modified production files

- `src/vienetts_app/core/models.py`
  - Adds validated performance profile preference.
- `src/vienetts_app/core/settings.py`
  - Persists the profile preference atomically.
- `src/vienetts_app/core/detector.py`
  - Carries broad hardware and core-count evidence without importing Torch at
    construction.
- `src/vienetts_app/core/engine.py`
  - Receives resolved thread value from the profile policy.
- `src/vienetts_app/ui/controller.py`
  - Owns asynchronous audio state, engine status, intent-triggered warmup,
    and profile selection.
- `src/vienetts_app/ui/bridge.py`
  - Publishes hardware note only after deferred detection completes.
- `src/vienetts_app/app.py`
  - Schedules deferred startup sequence after first frame and owns
    cancellation/lifetime guards.
- `src/vienetts_app/ui/qml/Main.qml`
  - Shows truthful status card and compact-nav tooltips.
- `src/vienetts_app/ui/qml/SettingsTab.qml`
  - Renders profile choices and state explanations.
- `src/vienetts_app/ui/qml/TextTab.qml`
  - Uses `TextMetrics`.
- `src/vienetts_app/ui/qml/ParagraphTab.qml`
  - Uses `TextMetrics`.

### Modified tests

- `tests/unit/test_models.py`
- `tests/unit/test_settings.py`
- `tests/unit/test_detector.py`
- `tests/unit/test_engine.py`
- `tests/unit/test_controller.py`
- `tests/unit/test_app_entry.py`
- `tests/smoke/test_ui_shell.py`
- `tests/smoke/test_ui_tabs.py`
- `tests/smoke/test_performance_harness.py`

---

### Task 1: Add deterministic resource-profile policy

**Files:**

- Create: `src/vienetts_app/core/resource_profiles.py`
- Create: `tests/unit/test_resource_profiles.py`
- Modify: `src/vienetts_app/core/models.py`
- Modify: `src/vienetts_app/core/settings.py`
- Modify: `tests/unit/test_models.py`
- Modify: `tests/unit/test_settings.py`

**Interfaces:**

- Produces:

```python
PerformanceProfile = Literal["auto", "performance", "efficiency"]

@dataclass(frozen=True)
class ResourceEvidence:
    hardware_kind: Literal["apple_silicon", "apple_intel", "nvidia", "other"]
    physical_cores: int

@dataclass(frozen=True)
class ResourcePlan:
    profile: PerformanceProfile
    onnx_threads: int
    reason: str

def physical_core_count(
    *, platform_name: str | None = None,
    sysctl: Callable[[list[str]], str] | None = None,
    cpu_count: Callable[[], int | None] = os.cpu_count,
) -> int: ...

def resolve_resource_plan(profile: PerformanceProfile, evidence: ResourceEvidence) -> ResourcePlan: ...
```

- `Settings.performance_profile: PerformanceProfile = "auto"`.

- [ ] **Step 1: Write failing policy and settings tests**

```python
from vienetts_app.core.resource_profiles import ResourceEvidence, resolve_resource_plan


def test_m4_seed_profiles_are_4_2_1_threads() -> None:
    evidence = ResourceEvidence(hardware_kind="apple_silicon", physical_cores=8)

    assert resolve_resource_plan("performance", evidence).onnx_threads == 4
    assert resolve_resource_plan("auto", evidence).onnx_threads == 2
    assert resolve_resource_plan("efficiency", evidence).onnx_threads == 1


def test_profile_threads_are_clamped_to_detected_physical_cores() -> None:
    evidence = ResourceEvidence(hardware_kind="other", physical_cores=1)

    assert resolve_resource_plan("performance", evidence).onnx_threads == 1
    assert resolve_resource_plan("auto", evidence).onnx_threads == 1
    assert resolve_resource_plan("efficiency", evidence).onnx_threads == 1


def test_settings_reject_unknown_performance_profile() -> None:
    with pytest.raises(ValueError, match="performance_profile"):
        Settings(performance_profile="turbo")
```

Complete the policy and persistence matrix with these assertions:

- mocked macOS `sysctl -n hw.physicalcpu` output `"8\n"` yields eight
  physical cores;
- mocked Linux and Windows probe fallbacks yield the injected physical-core
  count without invoking macOS `sysctl`;
- zero, negative, and nonnumeric probe results clamp to one core;
- serializing and reloading `Settings(performance_profile="efficiency")`
  preserves that choice;
- loading an existing settings JSON with no `performance_profile` key yields
  `"auto"`.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:

```bash
./.venv/bin/pytest -n 0 \
  tests/unit/test_resource_profiles.py \
  tests/unit/test_models.py \
  tests/unit/test_settings.py -v
```

Expected: FAIL because profile types and policy module do not exist.

- [ ] **Step 3: Implement profile policy**

Use only stdlib probes. On macOS, attempt:

```python
result = subprocess.run(
    ["sysctl", "-n", "hw.physicalcpu"],
    capture_output=True,
    check=False,
    text=True,
)
```

through the injectable `sysctl` seam. On other systems or a bad result, use
`os.cpu_count()` as a conservative upper bound and clamp it to at least 1.
Never invoke this probe from `Settings`, `TTSEngine.__init__`, or a QML
property getter.

Define the resolver in one table:

```python
_PROFILE_TARGETS = {
    "performance": 4,
    "auto": 2,
    "efficiency": 1,
}

def resolve_resource_plan(profile: PerformanceProfile, evidence: ResourceEvidence) -> ResourcePlan:
    target = _PROFILE_TARGETS[profile]
    threads = max(1, min(target, max(1, evidence.physical_cores)))
    return ResourcePlan(profile=profile, onnx_threads=threads,
                        reason=f"{profile} profile, {threads} ONNX thread(s)")
```

Keep profile labels/user wording out of this core module. Add
`performance_profile` validation in `Settings.__post_init__` and let existing
`asdict` settings persistence serialize it.

- [ ] **Step 4: Run focused policy tests**

Run:

```bash
./.venv/bin/pytest -n 0 \
  tests/unit/test_resource_profiles.py \
  tests/unit/test_models.py \
  tests/unit/test_settings.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit resource policy**

```bash
git add src/vienetts_app/core/resource_profiles.py src/vienetts_app/core/models.py \
  src/vienetts_app/core/settings.py tests/unit/test_resource_profiles.py \
  tests/unit/test_models.py tests/unit/test_settings.py
git commit -m "feat(perf): add bounded resource profiles"
git notes add -m "Phase 5 Task 1: added Auto, Performance, and Efficiency policy."
```

### Task 2: Apply profile evidence at engine construction

**Files:**

- Modify: `src/vienetts_app/core/detector.py`
- Modify: `src/vienetts_app/core/engine.py`
- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `tests/unit/test_detector.py`
- Modify: `tests/unit/test_engine.py`
- Modify: `tests/unit/test_controller.py`

**Interfaces:**

- Consumes: `ResourceEvidence`, `ResourcePlan`, and profile Settings.
- Produces:

```python
def resource_evidence_for(hardware: HardwareInfo, *,
                          physical_cores: int | None = None) -> ResourceEvidence: ...

class AppController(QObject):
    @Property(str, notify=performanceProfileChanged)
    def performanceProfile(self) -> str: ...
    @performanceProfile.setter
    def performanceProfile(self, value: str) -> None: ...
```

- `TTSEngine(..., threads: int | None)` remains the lower-level engine seam;
  only `AppController._ensure_worker` resolves and passes it.

- [ ] **Step 1: Write failing profile-to-engine tests**

```python
def test_controller_passes_resolved_auto_threads_to_new_engine(harness, monkeypatch) -> None:
    monkeypatch.setattr(
        "vienetts_app.ui.controller.resolve_resource_plan",
        lambda profile, evidence: ResourcePlan(profile, 2, "test"),
    )
    harness.controller.generate("Xin chào", "")

    assert harness.engines[-1].init_kwargs["threads"] == 2


def test_profile_change_after_engine_construction_requires_restart(harness) -> None:
    harness.controller.generate("Xin chào", "")

    harness.controller.performanceProfile = "efficiency"

    assert harness.controller.needsRestart is True
    assert harness.controller.performanceProfile == "efficiency"
```

Complete the profile-to-engine matrix with these assertions:

- explicitly selected Torch receives no profile-derived `threads` argument;
- a CPU/ONNX engine receives the selected plan’s thread count;
- assigning `"turbo"` preserves the prior profile and exposes a validation
  error;
- choosing a profile before the first engine is constructed leaves
  `needsRestart` false.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_detector.py \
  tests/unit/test_engine.py \
  tests/unit/test_controller.py -v
```

Expected: FAIL because controller does not expose or resolve the profile.

- [ ] **Step 3: Implement deferred evidence and engine wiring**

Extend `HardwareInfo` only with immutable broad classification and
physical-core count or add `resource_evidence_for`; do not cause
`detect_hardware()` to import Torch earlier than it does now. The controller
initially uses `ResourceEvidence(hardware_kind="other",
physical_cores=physical_core_count())` from a cached non-Torch probe. When
deferred hardware detection returns, update only future engine construction
evidence.

In `_ensure_worker`, use:

```python
threads: int | None = None
if self._settings.backend != "torch":
    plan = resolve_resource_plan(self._settings.performance_profile, self._resource_evidence)
    threads = plan.onnx_threads
self._engine = self._engine_factory(
    backend=self._resolved_backend_for_current_model(),
    precision=self._settings.precision,
    voices_dir=self._voices_dir,
    model_repo=self._settings.model_repo,
    managed_model=self._managed_model_or_none(),
    threads=threads,
)
```

Set `performanceProfileChanged` only after the existing `replace` plus
`save_settings` succeeds. Treat it as engine-affecting in `_set_setting`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_detector.py \
  tests/unit/test_engine.py \
  tests/unit/test_controller.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit profile-to-engine wiring**

```bash
git add src/vienetts_app/core/detector.py src/vienetts_app/core/engine.py \
  src/vienetts_app/ui/controller.py tests/unit/test_detector.py \
  tests/unit/test_engine.py tests/unit/test_controller.py
git commit -m "feat(engine): apply selected CPU resource profile"
git notes add -m "Phase 5 Task 2: wired profile evidence to future ONNX engine instances."
```

### Task 3: Defer audio, hardware, status, and useful-intent warmup

**Files:**

- Modify: `src/vienetts_app/app.py`
- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/ui/bridge.py`
- Create: `tests/unit/test_startup_deferred_work.py`
- Modify: `tests/unit/test_app_entry.py`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/smoke/test_ui_shell.py`

**Interfaces:**

- Produces:

```python
AudioState = Literal["checking", "available", "unavailable"]

class AppController(QObject):
    @Property(str, notify=audioStateChanged)
    def audioState(self) -> str: ...
    @Property(bool, notify=audioStateChanged)
    def audioAvailable(self) -> bool: ...
    @Property(str, notify=engineStateChanged)
    def engineState(self) -> str: ...
    @Slot()
    def refreshAudioAvailabilityAsync(self) -> None: ...
    @Slot(str)
    def requestWarmupForIntent(self, intent: str) -> None: ...
```

- `schedule_post_first_frame(window, callback)` in `app.py` connects a
  one-shot `frameSwapped` callback, with an exposed-window fallback timer for
  backends that do not deliver frame swaps.

- [ ] **Step 1: Write failing passive-startup tests**

```python
def test_create_app_does_not_probe_audio_or_start_warmup(qguiapp) -> None:
    probes: list[None] = []
    controller = AppController(audio_probe=lambda: probes.append(None) or True)

    _app, engine = create_app(controller_factory=lambda: controller)

    assert probes == []
    assert controller.audioState == "checking"
    assert controller.engineState == "checking"
    assert not controller.audioAvailable
    assert not engine._controller._worker


def test_post_first_frame_runs_each_deferred_action_once(qguiapp) -> None:
    calls: list[str] = []
    window = fake_window_with_frame_signal()

    schedule_post_first_frame(window, lambda: calls.append("after-frame"))
    window.frameSwapped.emit()
    window.frameSwapped.emit()

    assert calls == ["after-frame"]
```

Complete the deferred-startup matrix with these assertions:

- `audioState == "checking"` keeps the export-only banner hidden;
- a deferred injected probe publishes `"available"` or `"unavailable"` and
  updates `audioAvailable`;
- the hardware detector call count remains zero before `frameSwapped`;
- advancing a fake timer past 500 ms submits no warmup;
- stable text or EPUB intent submits one silent warmup only when the model is
  ready and the profile is not `"efficiency"`.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_startup_deferred_work.py \
  tests/unit/test_app_entry.py \
  tests/unit/test_controller.py \
  tests/smoke/test_ui_shell.py -v
```

Expected: FAIL because `audioAvailable` currently probes on property read and
`run_gui` schedules unconditional warmup.

- [ ] **Step 3: Make state getters passive**

Initialize controller fields:

```python
self._audio_state: AudioState = "checking"
self._engine_state = "checking"
self._warmup_submitted_for_engine = False
self._audio_probe_pending = False
```

`audioAvailable` returns `self._audio_state == "available"` only. It never
calls `_audio_probe`. `refreshAudioAvailabilityAsync` coalesces a pending
request and schedules a post-event-loop probe:

```python
def refreshAudioAvailabilityAsync(self) -> None:
    if self._audio_probe_pending:
        return
    self._audio_probe_pending = True
    QTimer.singleShot(0, self._run_audio_probe)

def _run_audio_probe(self) -> None:
    self._audio_probe_pending = False
    try:
        state: AudioState = "available" if self._audio_probe() else "unavailable"
    except Exception:
        logger.exception("audio availability probe failed")
        state = "unavailable"
    self._set_audio_state(state)
```

This is deferred asynchronously relative to first paint while retaining a
GUI-thread-safe QtMultimedia device probe. Do not dispatch Qt device
enumeration to a generic worker thread unless the platform capability test
later proves that safe.

Set `engineState` from model-manager outcomes and worker lifecycle:
`checking` before ModelManager inspect, `unavailable`/`downloading`/
`validating`/`ready`/`failed` from model status, and `initializing` while a
first real or warmup worker operation initializes the model. A completed
warmup or first successful artifact sets ready; a worker init failure sets
failed only if no model-manager failure status supersedes it.

- [ ] **Step 4: Schedule post-frame startup and intent warmup**

Add:

```python
def _after_first_frame() -> None:
    controller.refreshModelState()
    controller.refreshAudioAvailabilityAsync()
    bridge.resolve_engine_note_async()
```

Call it with the one-shot `schedule_post_first_frame` after `create_app`
returns the root QQuickWindow. Use a 750 ms single-shot fallback only when no
`frameSwapped` signal fires, and disconnect/cancel both callbacks during
shutdown. Remove the unconditional `QTimer.singleShot(500,
controller.prewarm_engine)`.

In `TextTab` and `ParagraphTab`, a 600 ms single-shot text-idle timer calls
`controller.requestWarmupForIntent("stable-text")` only when the editor has
nonblank text and no foreground job. In
`AudiobookController._on_epub_opened`, call it with `"epub-opened"` after
successful book opening. The controller checks:

```python
if (
    self._settings.performance_profile != "efficiency"
    and self.modelReady
    and not self._warmup_submitted_for_engine
    and self._worker is None
):
    self.prewarm_engine()
    self._warmup_submitted_for_engine = True
```

Reset the guard only when shutdown drops the engine. Do not surface a generic
error from a failed best-effort warmup.

- [ ] **Step 5: Run focused deferred-startup tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_startup_deferred_work.py \
  tests/unit/test_app_entry.py \
  tests/unit/test_controller.py \
  tests/smoke/test_ui_shell.py -v
```

Expected: PASS, with no audio probe or worker before the synthetic first
frame.

- [ ] **Step 6: Commit truthful deferred startup**

```bash
git add src/vienetts_app/app.py src/vienetts_app/ui/controller.py \
  src/vienetts_app/ui/bridge.py tests/unit/test_startup_deferred_work.py \
  tests/unit/test_app_entry.py tests/unit/test_controller.py \
  tests/smoke/test_ui_shell.py
git commit -m "fix(startup): defer probes and intent-based warmup"
git notes add -m "Phase 5 Task 3: made startup states passive and deferred all expensive work past first frame."
```

### Task 4: Expose profiles and truthful state in QML

**Files:**

- Modify: `src/vienetts_app/ui/qml/Main.qml`
- Modify: `src/vienetts_app/ui/qml/SettingsTab.qml`
- Modify: `tests/smoke/test_ui_shell.py`
- Modify: `tests/smoke/test_ui_tabs.py`

**Interfaces:**

- Consumes:
  - `controller.engineState`, `modelState`, `audioState`,
    `performanceProfile`, `needsRestart`.
- Produces QML object names:
  - `engineStatusBadge`
  - `engineStatusText`
  - `audioCheckingNotice`
  - `performanceProfileCombo`
  - `navTooltip`

- [ ] **Step 1: Write failing shell QML tests**

```python
def test_engine_card_is_not_ready_while_model_state_is_checking(shell) -> None:
    shell.controller.set_model_state_for_test("checking")
    badge = shell.find("engineStatusBadge")

    assert badge.property("text") != "Sẵn sàng"
    assert "Kiểm tra" in shell.find("engineStatusText").property("text")


def test_compact_nav_has_visible_tooltip_after_hover(shell) -> None:
    shell.window.setWidth(700)
    item = shell.nav_button_for("audiobook")
    shell.hover(item)

    assert shell.find("navTooltip").property("visible") is True


def test_profile_combo_exposes_only_three_safe_choices(shell) -> None:
    items = shell.find("performanceProfileCombo").property("model")
    assert [row["value"] for row in items] == ["auto", "performance", "efficiency"]
```

Complete the shell-state matrix with these assertions:

- `"checking"` hides `exportOnlyNotice`, while `"unavailable"` shows it;
- changing the profile through the combo persists the selected value through
  the controller;
- a downloading model with positive progress never renders a ready badge.

- [ ] **Step 2: Run focused QML tests to verify they fail**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/smoke/test_ui_shell.py tests/smoke/test_ui_tabs.py -v`

Expected: FAIL because Main.qml hardcodes the ready status and lacks profile
UI/tooltips.

- [ ] **Step 3: Implement status and profile presentation**

Replace the hardcoded engine-card green dot/“Sẵn sàng” with a local mapper:

```qml
function statusPresentation(state) {
    const items = {
        checking: ["info", qsTr("Đang kiểm tra")],
        unavailable: ["warning", qsTr("Cần tải mô hình")],
        downloading: ["info", qsTr("Đang tải mô hình")],
        validating: ["info", qsTr("Đang kiểm tra tệp")],
        initializing: ["info", qsTr("Đang khởi tạo")],
        ready: ["success", qsTr("Sẵn sàng")],
        failed: ["error", qsTr("Cần xử lý")]
    };
    return items[state] || items.checking;
}
```

Use `controller.engineState` as the card’s primary state and show
`modelState` detail when distinct. Make `exportOnlyNotice` visible only for
`controller.audioState === "unavailable"`; add a neutral
`audioCheckingNotice` only while checking.

Add a Settings row:

```qml
readonly property var performanceProfiles: [
    { value: "auto", label: qsTr("Tự động — cân bằng tốc độ và điện năng") },
    { value: "performance", label: qsTr("Hiệu năng — phản hồi nhanh nhất") },
    { value: "efficiency", label: qsTr("Tiết kiệm — giảm sử dụng CPU") }
]
```

Its combo writes `controller.performanceProfile`, explains it applies at the
next engine initialization, and does not display the resolved raw thread
value. The Settings model-source text must say official managed files live in
application data, not `~/.cache/huggingface/hub`.

Inside the nav `Button`, add:

```qml
ToolTip {
    id: navTooltip
    objectName: "navTooltip"
    visible: window.compactLayout && navButton.hovered
    text: navButton.modelData ? navButton.modelData.label : ""
    delay: 350
}
```

Retain `Accessible.name` and ensure labels remain visible in the wide rail.

- [ ] **Step 4: Run focused QML tests**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/smoke/test_ui_shell.py tests/smoke/test_ui_tabs.py -v`

Expected: PASS.

- [ ] **Step 5: Commit state/profile UI**

```bash
git add src/vienetts_app/ui/qml/Main.qml src/vienetts_app/ui/qml/SettingsTab.qml \
  tests/smoke/test_ui_shell.py tests/smoke/test_ui_tabs.py
git commit -m "feat(ui): show truthful engine states and profiles"
git notes add -m "Phase 5 Task 4: added state-aware shell status, safe profile picker, and compact nav tooltips."
```

### Task 5: Share debounced text metrics and verify scroll interaction

**Files:**

- Create: `src/vienetts_app/ui/qml/components/TextMetrics.qml`
- Modify: `src/vienetts_app/ui/qml/TextTab.qml`
- Modify: `src/vienetts_app/ui/qml/ParagraphTab.qml`
- Create: `scripts/benchmarks/run_scroll_handoff.py`
- Modify: `tests/smoke/test_ui_tabs.py`
- Modify: `tests/smoke/test_performance_harness.py`
- Modify: `docs/performance/README.md`

**Interfaces:**

- Produces QML `TextMetrics`:

```qml
QtObject {
    required property string sourceText
    property int wordCount: 0
    property int estimatedDurationSeconds: 0
    property real estimatedDurationMinutes: 0
    property int recomputeCount: 0
    property int debounceMs: 250
    function recompute() { ... }
}
```

- `run_scroll_handoff.py` prints JSONL records:

```json
{
  "scenario": "audiobook-chapter-list-boundary",
  "input": "wheel",
  "frames": 120,
  "frame_p95_ms": 16.7,
  "outer_delta_after_inner_end": 160,
  "pass": true
}
```

- [ ] **Step 1: Write failing metric and scroll-harness tests**

```python
def test_text_metrics_debounces_many_edits_to_one_full_scan(qml_metrics, qcoreapp) -> None:
    qml_metrics.setProperty("sourceText", "một hai")
    qml_metrics.setProperty("sourceText", "một hai ba")
    qml_metrics.setProperty("sourceText", "một hai ba bốn")
    spin_event_loop(qcoreapp, milliseconds=300)

    assert qml_metrics.property("wordCount") == 4
    assert qml_metrics.property("recomputeCount") == 1


def test_scroll_handoff_record_requires_outer_progress_at_inner_boundary() -> None:
    record = analyze_handoff(
        inner_positions=[0, 50, 100, 100],
        outer_positions=[0, 0, 0, 32],
        frame_ms=[16.0] * 120,
    )

    assert record["pass"] is True
```

Complete the metrics and interaction matrix with these assertions:

- `""`, `" \u00a0\t\n"`, and Unicode-separated Vietnamese words produce
  zero, zero, and the expected nonzero word counts;
- one word formats as seconds and a 150-word sample formats as minutes;
- both TextTab and ParagraphTab instantiate `TextMetrics`, while neither
  source contains a local `function countWords`;
- an `analyze_handoff` record with a saturated inner scroller and unchanged
  outer positions reports `pass is False`.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/smoke/test_ui_tabs.py \
  tests/smoke/test_performance_harness.py -v
```

Expected: FAIL because each tab currently scans text with a local regex and
the scroll analysis harness does not exist.

- [ ] **Step 3: Implement shared debounced metrics**

`TextMetrics.qml` owns the only full scan:

```qml
function recompute() {
    const trimmed = sourceText.trim();
    const matches = trimmed === "" ? null : trimmed.match(/\S+/g);
    wordCount = matches ? matches.length : 0;
    estimatedDurationSeconds = wordCount === 0 ? 0
        : Math.max(1, Math.round(wordCount / 2.5));
    estimatedDurationMinutes = wordCount === 0 ? 0 : wordCount / 150.0;
    recomputeCount += 1;
}

Timer {
    id: debounce
    interval: root.debounceMs
    repeat: false
    onTriggered: root.recompute()
}
onSourceTextChanged: debounce.restart()
Component.onCompleted: recompute()
```

Instantiate this once in each tab, bind sourceText to its editor text, and
replace every display binding that calls `countWords` or
`estimateDuration*` with cached component properties. Remove the duplicate
functions from both tabs.

- [ ] **Step 4: Implement deterministic scroll validation**

Use `create_app` with existing fake controller/audiobook/playback seams.
Locate `ScrollView` inner flickables through `property("contentItem")`, send
`QWheelEvent`/`QHoverEvent` via `QCoreApplication.sendEvent`, and record
`QQuickWindow.frameSwapped` timestamps when a real window is available.
For every scenario, emit phase markers and calculate:

```python
passed = (
    outer_positions[-1] > outer_positions[0]
    and percentile(frame_ms, 95) <= 16.7
    and max(frame_ms, default=0.0) <= 33.0
)
```

Scenarios are the page, chapter list at both boundaries, long documentation
area, text editor, and wheel storm. On supported desktop runners, run:

```bash
QSG_INFO=1 QSG_RENDER_TIMING=1 \
./.venv/bin/python scripts/benchmarks/run_scroll_handoff.py --iterations 3
```

If any scenario’s `pass` is false, create a Beads issue using the exact
recorded scenario/trace before modifying QML scrolling. If all pass, add only
the benchmark result/documentation and preserve existing handlers.

- [ ] **Step 5: Run focused tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/smoke/test_ui_tabs.py \
  tests/smoke/test_performance_harness.py -v
```

Expected: PASS. Headless test records may report `frame_signal_supported:
false`, which is skipped rather than falsely passed.

- [ ] **Step 6: Commit metrics and scroll verification**

```bash
git add src/vienetts_app/ui/qml/components/TextMetrics.qml \
  src/vienetts_app/ui/qml/TextTab.qml src/vienetts_app/ui/qml/ParagraphTab.qml \
  scripts/benchmarks/run_scroll_handoff.py tests/smoke/test_ui_tabs.py \
  tests/smoke/test_performance_harness.py docs/performance/README.md
git commit -m "perf(ui): debounce text metrics and validate scrolling"
git notes add -m "Phase 5 Task 5: deduplicated document metrics and added reproducible scroll-handoff evidence."
```

### Task 6: Record profile evidence and complete Phase 5 verification

**Files:**

- Create: `scripts/benchmarks/run_profiles.py`
- Modify: `scripts/benchmarks/schema.py`
- Modify: `scripts/benchmarks/summarize.py`
- Modify: `tests/unit/test_benchmark_schema.py`
- Modify: `tests/unit/test_benchmark_statistics.py`
- Modify: `docs/performance/README.md`
- Modify: `README.md`
- Modify: `PROJECT_PLAN.md`

**Interfaces:**

- Produces benchmark tags:
  - `profile`
  - `resolved_onnx_threads`
  - `physical_cores`
  - `hardware_kind`
  - `measurement_method`
  - `process_cold_startup_ms`, `model_warm_ttfc_ms`,
    `first_sink_write_ms`, `peak_rss_bytes`, `event_loop_p95_ms`

- [ ] **Step 1: Write failing benchmark schema tests**

```python
def test_profile_benchmark_record_requires_resource_evidence() -> None:
    with pytest.raises(ValueError, match="resolved_onnx_threads"):
        BenchmarkRecord.from_dict(
            {
                "benchmark": "profile-pipeline",
                "tags": {"profile": "auto"},
                "measurements": {"model_warm_ttfc_ms": 100.0},
            }
        )
```

Add a test that a record serializes no `source_text`, `voice`, `path`,
`audio`, `token`, or `credential` field and tests profile labels against the
three allowed values.

- [ ] **Step 2: Run focused schema tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_benchmark_schema.py tests/unit/test_benchmark_statistics.py -v`

Expected: FAIL because profile resource fields are not required by the schema.

- [ ] **Step 3: Implement the profile matrix runner**

For each profile, run at least five repetitions per supported benchmark
method. Use the existing corpus hash rather than source text. Resolve the
same `ResourcePlan` passed to `TTSEngine`, record both requested profile and
resolved threads, and run process-cold startup through the fresh-child
protocol established in Phase 1. Keep profile output in a user-supplied
ignored directory, for example:

```bash
./.venv/bin/python scripts/benchmarks/run_profiles.py \
  --iterations 5 \
  --output build/benchmarks/profiles.jsonl
```

`run_profiles.py` fails closed if model state is not ready or if a record
lacks required machine/resource tags. It does not download models, run local
calibration, or write model data.

- [ ] **Step 4: Run focused benchmark tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_benchmark_schema.py tests/unit/test_benchmark_statistics.py -v`

Expected: PASS.

- [ ] **Step 5: Run final Phase 5 quality gate**

Run:

```bash
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest
```

Expected: all commands exit 0.

- [ ] **Step 6: Document measured boundaries and commit**

Document the three profiles as user intent rather than guarantees, M4 seed
values, platform-specific measurement requirement, deferred startup sequence,
audio tri-state, explicit no-auto-calibration decision, and scroll-harness
pass criteria. Update targets only after benchmark output is retained under
the privacy-safe schema.

```bash
git add scripts/benchmarks/run_profiles.py scripts/benchmarks/schema.py \
  scripts/benchmarks/summarize.py tests/unit/test_benchmark_schema.py \
  tests/unit/test_benchmark_statistics.py docs/performance/README.md README.md \
  PROJECT_PLAN.md
git commit -m "docs(perf): record resource profile measurements"
git notes add -m "Phase 5 Task 6: completed profile evidence and startup/UX verification."
```
