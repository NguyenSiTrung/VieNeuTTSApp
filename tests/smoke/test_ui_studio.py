"""Offscreen Studio smoke suite (FR-4.3, Task 4.4).

Drives the real GUI (create_app + Main.qml + StudioTab.qml) under
``QT_QPA_PLATFORM=offscreen`` with the REAL AppController over fakes at its
seams: a duck-typed engine/worker (no model), a recording file player and a
stub audio sink. One subprocess per test group (one QGuiApplication per
process), reporting a ``RESULT:``-prefixed JSON line, as in
``test_ui_tabs.py``.

Covered: the timeline (clips as proportional blocks, selection band,
playhead), the history chips with undo/reset, and the Hiệu ứng panel.
The panel's sliders stage edits through the pending seam (``studioStage*``)
and never push. Its pending list unstages per row. "Bỏ" clears the list.
"Áp dụng N thay đổi" is the screen's only primary and commits the staged
edits as ONE undo step. The Gốc / Đã chỉnh switch drives
``studioCompareMode``. Also covered: the empty state's links to Tạo giọng
đọc and its Tài liệu mode; the one-instance re-parenting of the panel
between the wide side column and the stacked slot; the 44 px / 12 px floors.

Slider drags are simulated the way a user drag lands. The driver writes
``value`` from C++, which leaves the QML ``value:`` binding in place, then
emits ``moved``. Native dialogs stay closed headless (same policy as
test_ui_tabs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

DRIVER = textwrap.dedent(
    """\
    import json
    import sys
    from pathlib import Path

    import numpy as np
    from PySide6.QtCore import QMetaObject, QObject, QPointF, Signal, Slot

    import vienetts_app.ui.controller as _controller_module
    from vienetts_app.app import create_app, wait_for_tabs
    from vienetts_app.core.artifacts import SynthesisArtifact
    from vienetts_app.core.audio import write_wav_file
    from vienetts_app.core.detector import HardwareInfo
    from vienetts_app.ui.audiobook_controller import AudiobookController
    from vienetts_app.ui.bg_ops import run_sync
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.chapter_persist import SyncPersistExecutor
    from vienetts_app.ui.controller import AppController
    from vienetts_app.ui.stream_playback import StreamPlaybackController

    tmp = Path(sys.argv[1])
    scenarios = sys.argv[2].split(",")


    class _ViLocale:
        # Vietnamese UI copy regardless of the host locale.
        @staticmethod
        def system():
            return _ViLocale()

        def name(self):
            return "vi_VN"


    _controller_module.QLocale = _ViLocale


    class FakeEngine:
        def __init__(self, **kwargs):
            self.sample_rate = 48_000

        def close(self):
            pass


    class FakeWorker(QObject):
        progress = Signal(object)
        chunk_ready = Signal(object)
        terminal = Signal(object)

        def __init__(self, engine):
            super().__init__()
            self.engine = engine

        def start(self):
            pass

        def submit(self, job):
            return True

        def cancel_job(self, job_id):
            return True

        def cancel_owner(self, owner):
            return 0

        def stop(self, timeout_ms: int = 5000):
            pass


    class FakePlayback(QObject):
        def __init__(self):
            super().__init__()
            self.played = []

        @Slot(str)
        def play(self, path):
            self.played.append(str(path))

        @Slot()
        def stop(self):
            pass

        @Slot()
        def pause(self):
            pass

        @Slot()
        def resume(self):
            pass


    class FakeSink:
        def __init__(self):
            self._state = "StoppedState"

        def start(self, device):
            self._state = "ActiveState"

        def stop(self):
            self._state = "StoppedState"

        def state(self):
            return self._state


    sink = FakeSink()
    controller = AppController(
        data_dir=tmp,
        engine_factory=lambda **kwargs: FakeEngine(**kwargs),
        worker_factory=lambda engine: FakeWorker(engine),
        catalog=lambda: [],
        saved_names=lambda voices_dir: [],
        bg_runner=run_sync,
        audio_probe=lambda: True,
        stream_playback_factory=lambda: StreamPlaybackController(sink_factory=lambda _fmt: sink),
        hardware_probe=lambda: HardwareInfo(kind="none", torch_installed=False, cuda_version=None),
    )
    playback = FakePlayback()
    bridge = ShellBridge()
    app, engine = create_app(
        bridge_factory=lambda: bridge,
        controller_factory=lambda: controller,
        playback_factory=lambda: playback,
        audiobook_factory=lambda app_controller: AudiobookController(
            app_controller,
            data_dir=tmp,
            bg_runner=run_sync,
            persist_executor=SyncPersistExecutor(),
        ),
    )
    window = engine.rootObjects()[0]
    assert wait_for_tabs(app, window), "tab Loaders never became ready"
    window.setProperty("width", 1400)
    window.setProperty("height", 900)
    bridge.setCurrentTab("studio")
    app.processEvents()

    studio_tab = window.findChildren(QObject, "studioTab")[0]


    def settle():
        for _ in range(4):
            app.processEvents()


    def walk(root):
        # Repeater delegates have no QObject parent: only childItems() sees them.
        out, stack = [], [root]
        while stack:
            it = stack.pop()
            out.append(it)
            stack.extend(it.childItems())
        return out


    def items(name):
        found = [i for i in walk(studio_tab) if i.objectName() == name]
        found.sort(key=lambda i: (float(i.mapToScene(QPointF(0, 0)).y()),
                                  float(i.mapToScene(QPointF(0, 0)).x())))
        return found


    def item(name):
        found = items(name)
        assert found, name
        return found[0]


    def obj(name):
        found = studio_tab.findChildren(QObject, name)
        return found[0] if found else None


    def click(it):
        QMetaObject.invokeMethod(it, "click")
        settle()


    def drag(slider, value):
        # A user drag: C++ writes value (the QML binding survives), then moved.
        slider.setProperty("value", value)
        slider.moved.emit()
        settle()


    def visible_in_tab(it):
        node = it
        while node is not None:
            if not node.property("visible"):
                return False
            if node is studio_tab:
                return True
            node = node.parentItem()
        return False


    def scene_x(it):
        return float(it.mapToScene(QPointF(0, 0)).x())


    def jsv(value):
        return value.toVariant() if hasattr(value, "toVariant") else value


    results = {}

    for scenario in scenarios:
        out = {}
        if scenario == "empty_state":
            out["op_stack_visible"] = bool(item("studioOpStack").property("visible"))
            cards = [c for c in items("studioGuideCard") if visible_in_tab(c)]
            out["guide_cards"] = len(cards)
            texts = [str(i.property("text")) for i in walk(studio_tab)
                     if visible_in_tab(i) and isinstance(i.property("text"), str)]
            out["stale_copy"] = sorted(t for t in texts if "Tab " in t or "tab " in t)
            out["guide_titles"] = [str(i.property("text")) for i in items("studioGuideTitle")]
            bridge.setCreateMode("subtitles")
            bridge.setCurrentTab("studio")
            settle()
            click(item("studioGuideComposeButton"))
            out["compose_nav"] = [bridge.currentTab, bridge.createMode]
            bridge.setCurrentTab("studio")
            settle()
            click(item("studioGuideDocumentButton"))
            out["document_nav"] = [bridge.currentTab, bridge.createMode]
            bridge.setCurrentTab("studio")
            settle()
            click(item("studioGuideAudiobookButton"))
            out["audiobook_nav"] = bridge.currentTab
            bridge.setCurrentTab("studio")
            settle()

        elif scenario == "open":
            wav = write_wav_file(
                (0.5 * np.sin(2 * np.pi * 440.0 * np.arange(48_000) / 48_000)).astype(np.float32),
                tmp / "art.wav",
            )
            controller._current_artifact = SynthesisArtifact(
                job_id="a" * 32, path=wav, sample_rate=48_000, samples=48_000, duration_ms=1000
            )
            out["opened"] = bool(controller.openInStudio(
                "text", "first short\\n\\nsecond paragraph is much longer than the first one"
            ))
            settle()
            names = {o.objectName() for o in studio_tab.findChildren(QObject)}
            names |= {i.objectName() for i in walk(studio_tab)}
            out["missing"] = sorted({
                # Contract kept from the previous Studio (test_ui_tabs).
                "studioWaveform", "studioOpStack", "studioClipList",
                "studioPreviewButton", "studioExportButton", "studioQuickExportButton",
                "studioTransportDock", "studioSelectionBar", "studioDockTarget",
                "studioClipRow", "studioClipPlayButton", "studioRegenButton",
                "studioDeleteClipButton", "studioClipProfile", "studioOpHistoryCard",
                "studioOpBaseChip", "studioUndoButton", "studioResetButton",
                "studioGainSlider", "studioSpeedSlider", "studioGapSlider",
                "studioShortcutPlay", "studioShortcutStop",
                "studioShortcutSeekBack", "studioShortcutSeekForward",
                "studioRegenProfileBanner",
                # New in the rebuild.
                "studioTimeline", "studioTimelineClip", "studioTimelineSelection",
                "studioTimelinePlayhead", "studioTimeRuler", "studioFadeInSlider",
                "studioFadeOutSlider", "studioNormalizeToggle", "studioSilenceToggle",
                "studioCompareSwitch", "studioApplyButton", "studioClearPendingButton",
                "studioApplyBar", "studioPendingList",
            } - names)
            out["op_stack_visible"] = bool(item("studioOpStack").property("visible"))
            out["op_stack_count"] = len(items("studioOpStack"))

            # Timeline: one block per clip, widths proportional to duration.
            clips = jsv(controller.studioClips)
            blocks = items("studioTimelineClip")
            out["blocks"] = len(blocks)
            out["block_labels"] = [str(b.property("labelText")) for b in blocks]
            widths = [float(b.property("width")) for b in blocks]
            out["width_ratio"] = widths[0] / widths[1] if len(widths) == 2 else -1
            out["duration_ratio"] = clips[0]["duration"] / clips[1]["duration"]
            out["blocks_ordered"] = scene_x(blocks[0]) < scene_x(blocks[1])

            lane = item("studioTimelineLane")
            lane_w = float(lane.property("width"))
            waveform = item("studioWaveform")
            sel = item("studioTimelineSelection")
            out["selection_hidden"] = not bool(sel.property("visible"))
            waveform.selectionChanged.emit(0.25, 0.75)
            settle()
            out["selection_visible"] = bool(sel.property("visible"))
            out["selection_x_frac"] = float(sel.property("x")) / lane_w
            out["selection_w_frac"] = float(sel.property("width")) / lane_w
            out["selection_label"] = str(item("studioSelectionLabel").property("text"))
            out["selection_bar_visible"] = bool(item("studioSelectionBar").property("visible"))
            click(item("studioTrimSelectionButton"))
            out["ops_after_trim"] = [o["kind"] for o in jsv(controller.studioOps)]
            out["selection_cleared"] = not bool(sel.property("visible"))
            controller.studioUndo()
            settle()

            head = item("studioTimelinePlayhead")
            out["playhead_hidden_idle"] = not bool(head.property("visible"))
            out["preview"] = bool(controller.studioPreview())
            controller._set_replay_position(0.5)
            settle()
            out["playhead_visible"] = bool(head.property("visible"))
            out["playhead_frac"] = float(head.property("x")) / lane_w
            out["timecode"] = str(item("studioTimecode").property("text"))
            controller.stopReplay()
            settle()
            out["playhead_hidden_after_stop"] = not bool(head.property("visible"))

            # Clip table rows mirror the clip model.
            out["clip_rows"] = len(items("studioClipRow"))
            out["clip_numbers"] = [str(i.property("text")) for i in items("studioClipNumber")]

        elif scenario == "effects":
            def primaries():
                return [i for i in walk(studio_tab)
                        if i.property("variant") == "primary" and visible_in_tab(i)]

            apply_btn = item("studioApplyButton")
            out["primaries_idle"] = [str(p.objectName()) for p in primaries()]
            out["apply_text_idle"] = str(apply_btn.property("text"))
            out["apply_enabled_idle"] = bool(apply_btn.property("enabled"))
            out["clear_enabled_idle"] = bool(item("studioClearPendingButton").property("enabled"))
            out["preview_variant"] = str(item("studioPreviewButton").property("variant"))
            out["export_variant"] = str(item("studioExportButton").property("variant"))

            gain = item("studioGainSlider")
            drag(gain, 3.0)
            out["pending_after_gain"] = int(controller.studioPendingCount)
            out["ops_after_gain"] = len(jsv(controller.studioOps))
            out["gain_value"] = float(gain.property("value"))
            out["gain_readout"] = str(item("studioGainReadout").property("text"))
            out["apply_text_1"] = str(apply_btn.property("text"))
            out["apply_enabled_1"] = bool(apply_btn.property("enabled"))

            click(item("studioNormalizeToggle"))
            out["normalize_checked"] = bool(item("studioNormalizeToggle").property("checked"))
            drag(item("studioFadeInSlider"), 300)
            drag(item("studioSpeedSlider"), 1.25)
            out["pending_keys"] = [r["key"] for r in jsv(controller.studioPendingOps)]
            out["apply_text_4"] = str(apply_btn.property("text"))
            out["pending_rows"] = len(items("studioPendingRow"))
            out["primaries_pending"] = [str(p.objectName()) for p in primaries()]

            # Per-row unstage: drop the normalize row.
            rows = items("studioPendingRow")
            target = [r for r in rows if r.property("rowKey") == "normalize"][0]
            unstage = [i for i in walk(target) if i.objectName() == "studioUnstageButton"][0]
            out["unstage_label"] = str(unstage.property("accessibleLabel"))
            click(unstage)
            out["keys_after_unstage"] = [r["key"] for r in jsv(controller.studioPendingOps)]
            out["normalize_unchecked"] = not bool(item("studioNormalizeToggle").property("checked"))

            # Bỏ: everything staged is dropped and the sliders fall back.
            click(item("studioClearPendingButton"))
            out["pending_after_clear"] = int(controller.studioPendingCount)
            out["gain_after_clear"] = float(gain.property("value"))
            out["speed_after_clear"] = float(item("studioSpeedSlider").property("value"))

            # Stage two, apply: ONE history step for both, one undo reverts it.
            drag(gain, 2.0)
            click(item("studioSilenceToggle"))
            click(apply_btn)
            out["ops_after_apply"] = [o["kind"] for o in jsv(controller.studioOps)]
            out["pending_after_apply"] = int(controller.studioPendingCount)
            chips = items("studioOpChip")
            out["chips"] = len(chips)
            out["chip_checked"] = [bool(c.property("checked")) for c in chips]
            out["base_chip_checked"] = bool(item("studioOpBaseChip").property("checked"))
            undo = item("studioUndoButton")
            out["undo_enabled"] = bool(undo.property("enabled"))
            out["undo_tooltip"] = str(undo.property("tooltipText"))
            click(undo)
            out["ops_after_undo"] = len(jsv(controller.studioOps))
            out["base_chip_checked_after_undo"] = bool(
                item("studioOpBaseChip").property("checked"))
            out["undo_enabled_after_undo"] = bool(undo.property("enabled"))

            # A history chip reverts to its step.
            drag(gain, 1.0)
            click(apply_btn)
            # One op in the step: the tooltip names it.
            out["undo_tooltip_single"] = str(undo.property("tooltipText"))
            drag(item("studioGapSlider"), 900)
            click(apply_btn)
            click(items("studioOpChip")[0])
            out["ops_after_chip"] = [o["kind"] for o in jsv(controller.studioOps)]
            out["undo_tooltip_after_chip"] = str(undo.property("tooltipText"))

            # Reset asks first, then drops the stack.
            click(item("studioResetButton"))
            dialog = window.findChildren(QObject, "studioResetDialog")[0]
            out["reset_dialog_open"] = bool(dialog.property("opened")) or bool(
                dialog.property("visible"))
            confirm = window.findChildren(QObject, "studioResetConfirmButton")[0]
            click(confirm)
            out["ops_after_reset"] = len(jsv(controller.studioOps))
            # An empty stack after a reset is still one undoable step.
            out["undo_enabled_after_reset"] = bool(undo.property("enabled"))
            click(undo)
            out["ops_after_reset_undo"] = [o["kind"] for o in jsv(controller.studioOps)]

            # A/B listen target.
            switch = item("studioCompareSwitch")
            out["compare_initial"] = str(switch.property("currentValue"))
            click(item("studioCompareSwitch_base"))
            out["compare_after_click"] = [
                str(controller.studioCompareMode), str(switch.property("currentValue"))]
            controller.studioSetCompareMode("pending")
            settle()
            out["compare_after_controller"] = str(switch.property("currentValue"))

        elif scenario == "responsive":
            panel = item("studioOpStack")
            bar = item("studioApplyBar")
            out["wide_side_visible"] = bool(item("studioEffectsSide").property("visible"))
            out["wide_panel_right_of_timeline"] = scene_x(panel) > scene_x(
                item("studioTransportDock")) + 100
            window.setProperty("width", 700)
            settle()
            out["narrow_side_visible"] = bool(item("studioEffectsSide").property("visible"))
            out["narrow_slot_visible"] = bool(item("studioEffectsStackSlot").property("visible"))
            out["narrow_panel_visible"] = visible_in_tab(panel) and visible_in_tab(bar)
            out["panel_count"] = len(items("studioOpStack"))
            out["bar_count"] = len(items("studioApplyBar"))
            out["narrow_panel_below"] = float(panel.mapToScene(QPointF(0, 0)).y()) > float(
                item("studioClipList").mapToScene(QPointF(0, 0)).y())
            # Floors: every visible button >= 44 px, every visible text >= 12 px.
            small_targets, small_text = [], []
            for i in walk(studio_tab):
                if not visible_in_tab(i):
                    continue
                if i.property("variant") is not None and i.property("accessibleLabel") is not None:
                    if float(i.property("height")) < 44 or float(i.property("width")) < 44:
                        small_targets.append(i.objectName() or str(i.property("text")))
                font = i.property("font")
                if font is not None and hasattr(font, "pixelSize") and i.property("text"):
                    px = font.pixelSize()
                    if 0 < px < 12:
                        small_text.append(str(i.property("text")))
            out["small_targets"] = small_targets
            out["small_text"] = small_text
            window.setProperty("width", 1400)
            settle()

        results[scenario] = out

    controller.shutdown()
    print("RESULT:" + json.dumps(results))
    """
)


def run_driver(tmp_path: Path, scenarios: list[str]) -> dict[str, dict]:
    driver_path = tmp_path / "_studio_driver.py"
    driver_path.write_text(DRIVER, encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": os.pathsep.join([str(repo_root), str(repo_root / "src")]),
    }
    proc = subprocess.run(
        [sys.executable, str(driver_path), str(tmp_path), ",".join(scenarios)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


@pytest.fixture(scope="module")
def studio_results(tmp_path_factory) -> dict[str, dict]:
    tmp_path = tmp_path_factory.mktemp("studio")
    return run_driver(tmp_path, ["empty_state", "open", "effects", "responsive"])


@pytest.mark.slow
class TestStudioSmoke:
    def test_empty_state_points_at_the_new_destinations(self, studio_results) -> None:
        r = studio_results["empty_state"]
        assert r["op_stack_visible"] is False
        assert r["guide_cards"] == 3
        # No "Tab Văn bản" / "Tab Đoạn văn" leftovers from the old shell.
        assert r["stale_copy"] == []
        assert r["guide_titles"] == ["Tạo giọng đọc", "Tài liệu", "Sách nói"]
        assert r["compose_nav"] == ["create", "compose"]
        assert r["document_nav"] == ["create", "document"]
        assert r["audiobook_nav"] == "audiobook"

    def test_timeline_blocks_selection_and_playhead(self, studio_results) -> None:
        r = studio_results["open"]
        assert r["opened"] is True
        assert r["missing"] == []
        assert r["op_stack_visible"] is True
        assert r["op_stack_count"] == 1
        assert r["blocks"] == 2
        assert r["block_labels"] == ["Đoạn 1", "Đoạn 2"]
        assert r["blocks_ordered"] is True
        assert r["width_ratio"] == pytest.approx(r["duration_ratio"], rel=0.08)
        assert r["selection_hidden"] is True
        assert r["selection_visible"] is True
        assert r["selection_x_frac"] == pytest.approx(0.25, abs=0.02)
        assert r["selection_w_frac"] == pytest.approx(0.5, abs=0.02)
        assert r["selection_label"] == "Vùng chọn: 0:00 – 0:01"
        assert r["selection_bar_visible"] is True
        assert r["ops_after_trim"] == ["trim"]
        assert r["selection_cleared"] is True
        assert r["playhead_hidden_idle"] is True
        assert r["preview"] is True
        assert r["playhead_visible"] is True
        assert r["playhead_frac"] == pytest.approx(0.5, abs=0.03)
        assert r["timecode"].endswith("/ 0:01")
        assert r["playhead_hidden_after_stop"] is True
        assert r["clip_rows"] == 2
        assert r["clip_numbers"] == ["01", "02"]

    def test_effects_panel_stages_and_applies_once(self, studio_results) -> None:
        r = studio_results["effects"]
        # The apply button is the screen's ONE primary, idle or not.
        assert r["primaries_idle"] == ["studioApplyButton"]
        assert r["primaries_pending"] == ["studioApplyButton"]
        assert r["preview_variant"] != "primary"
        assert r["export_variant"] != "primary"
        assert r["apply_text_idle"] == "Áp dụng thay đổi"
        assert r["apply_enabled_idle"] is False
        assert r["clear_enabled_idle"] is False
        # A drag stages; nothing is pushed until Áp dụng.
        assert r["pending_after_gain"] == 1
        assert r["ops_after_gain"] == 0
        assert r["gain_value"] == 3.0
        assert r["gain_readout"] == "+3.0 dB"
        assert r["apply_text_1"] == "Áp dụng 1 thay đổi"
        assert r["apply_enabled_1"] is True
        assert r["normalize_checked"] is True
        assert r["pending_keys"] == ["gain", "normalize", "fade_in", "speed"]
        assert r["apply_text_4"] == "Áp dụng 4 thay đổi"
        assert r["pending_rows"] == 4
        assert r["unstage_label"] == "Bỏ thay đổi Chuẩn hóa"
        assert r["keys_after_unstage"] == ["gain", "fade_in", "speed"]
        assert r["normalize_unchecked"] is True
        assert r["pending_after_clear"] == 0
        assert r["gain_after_clear"] == 0.0
        assert r["speed_after_clear"] == 1.0
        # One apply = one history step for both edits.
        assert r["ops_after_apply"] == ["gain", "silence"]
        assert r["pending_after_apply"] == 0
        assert r["chips"] == 2
        assert r["chip_checked"] == [False, True]
        assert r["base_chip_checked"] is False
        assert r["undo_enabled"] is True
        # Two ops in the step: no single name, so a plain "Hoàn tác".
        assert r["undo_tooltip"] == "Hoàn tác"
        assert r["ops_after_undo"] == 0
        assert r["base_chip_checked_after_undo"] is True
        assert r["undo_enabled_after_undo"] is False
        assert r["undo_tooltip_single"] == "Hoàn tác: Khuếch đại"
        assert r["ops_after_chip"] == ["gain"]
        assert r["undo_tooltip_after_chip"] == "Hoàn tác"
        assert r["reset_dialog_open"] is True
        assert r["ops_after_reset"] == 0
        assert r["undo_enabled_after_reset"] is True
        assert r["ops_after_reset_undo"] == ["gain"]
        assert r["compare_initial"] == "pending"
        assert r["compare_after_click"] == ["base", "base"]
        assert r["compare_after_controller"] == "pending"

    def test_panel_reparents_and_floors_hold(self, studio_results) -> None:
        r = studio_results["responsive"]
        assert r["wide_side_visible"] is True
        assert r["wide_panel_right_of_timeline"] is True
        assert r["narrow_side_visible"] is False
        assert r["narrow_slot_visible"] is True
        assert r["narrow_panel_visible"] is True
        assert r["panel_count"] == 1
        assert r["bar_count"] == 1
        assert r["narrow_panel_below"] is True
        assert r["small_targets"] == []
        assert r["small_text"] == []
