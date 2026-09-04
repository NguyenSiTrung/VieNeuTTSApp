"""ShellBridge: QML-exposed shell state (FR-2.2/FR-2.5/FR-2.7).

The bridge is a plain QObject: properties are Python attributes plus
synchronous Signal emissions, so direct calls suffice — no event-loop
pumping, no cross-thread queuing. Fakes are injected for the settings dir,
the engine-note detector (proving no TTSEngine is ever built, NFR-2.1), and
the system-theme probe.
"""

from dataclasses import replace
from pathlib import Path

from vienetts_app.core.models import Settings
from vienetts_app.core.settings import SETTINGS_FILENAME, load_settings, save_settings
from vienetts_app.ui import bridge as bridge_mod
from vienetts_app.ui.bridge import TABS, ShellBridge

NOTE = "ONNX Runtime CPU · fake detector note"


class FakeSystemTheme:
    """Callable standing in for ui.theme.qt_system_theme; flippable."""

    def __init__(self, value: str = "dark") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class RecordingDetector:
    """Callable standing in for the detector seam; counts its calls."""

    def __init__(self, note: str = NOTE) -> None:
        self.note = note
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return self.note


class BridgeHarness:
    """Bridge wired to recording signal sinks (connected post-construction)."""

    def __init__(self, tmp_path: Path, note: str = NOTE, system: str = "dark") -> None:
        self.detector = RecordingDetector(note)
        self.system_theme = FakeSystemTheme(system)
        self.events: dict[str, list[str]] = {"tab": [], "preference": [], "effective": []}
        self.bridge = ShellBridge(
            settings_dir=tmp_path, detector=self.detector, system_theme=self.system_theme
        )
        self.bridge.currentTabChanged.connect(lambda: self.events["tab"].append("fired"))
        self.bridge.themePreferenceChanged.connect(
            lambda: self.events["preference"].append("fired")
        )
        self.bridge.effectiveThemeChanged.connect(lambda: self.events["effective"].append("fired"))

    def fired(self, name: str) -> int:
        return len(self.events[name])


class TestInitialState:
    def test_initial_state_defaults_and_preference(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path, system="light")
        assert h.bridge.currentTab == "text"
        assert h.bridge.themePreference == "system"
        assert h.bridge.effectiveTheme == "light"
        assert h.fired("tab") == 0
        assert h.fired("preference") == 0
        assert h.fired("effective") == 0
        assert BridgeHarness(tmp_path, system="dark").bridge.effectiveTheme == "dark"

        save_settings(Settings(theme="dark"), tmp_path)
        h_dark = BridgeHarness(tmp_path, system="light")
        assert h_dark.bridge.themePreference == "dark"
        assert h_dark.bridge.effectiveTheme == "dark"  # explicit beats system

    def test_engine_note_from_injected_detector(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path, note="PyTorch · CUDA 12.8 · batched")
        h.bridge.resolve_engine_note()
        assert h.bridge.engineNote == "PyTorch · CUDA 12.8 · batched"
        assert h.detector.calls == 1  # probed exactly once per resolve, no retries


class TestTabsApi:
    def test_tabs_api_and_selection(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        assert TABS == (
            ("text", "Văn bản"),
            ("paragraph", "Đoạn văn"),
            ("audiobook", "Sách nói"),
            ("cloning", "Sao chép giọng"),
            ("settings", "Cài đặt"),
        )
        assert h.bridge.tabs == [
            {"id": "text", "label": "Văn bản"},
            {"id": "paragraph", "label": "Đoạn văn"},
            {"id": "audiobook", "label": "Sách nói"},
            {"id": "cloning", "label": "Sao chép giọng"},
            {"id": "settings", "label": "Cài đặt"},
        ]
        for tab_id, _ in TABS:
            h.bridge.setCurrentTab(tab_id)
            assert h.bridge.currentTab == tab_id
        assert h.fired("tab") == len(TABS) - 1

        fired = []
        h.bridge.tabsChanged.connect(lambda: fired.append(True))
        h.bridge.refreshTabs()
        assert fired == [True]
        assert h.bridge.tabs[0] == {"id": "text", "label": "Văn bản"}


class TestCurrentTab:
    def test_current_tab_transitions_and_validation(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        h.bridge.setCurrentTab("settings")
        assert h.bridge.currentTab == "settings"
        assert h.fired("tab") == 1

        h.bridge.currentTab = "cloning"
        assert h.bridge.currentTab == "cloning"
        assert h.fired("tab") == 2

        # Same tab emits nothing
        h.bridge.setCurrentTab("cloning")
        assert h.fired("tab") == 2

        # Invalid tabs rejected
        for bad in ("banana", "", "Text", "text ", None, 3):
            h.bridge.setCurrentTab(bad)  # type: ignore[arg-type]
            assert h.bridge.currentTab == "cloning"
            assert h.fired("tab") == 2


class TestThemePreference:
    def test_set_persists_and_reresolves(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path, system="dark")  # "system" → effective "dark"
        h.bridge.themePreference = "light"
        assert h.bridge.themePreference == "light"
        assert h.bridge.effectiveTheme == "light"
        assert load_settings(tmp_path).theme == "light"  # round-trip via core/settings
        assert h.fired("preference") == 1
        assert h.fired("effective") == 1

    def test_explicit_to_system_follows_injected_system(self, tmp_path: Path) -> None:
        save_settings(Settings(theme="light"), tmp_path)
        h = BridgeHarness(tmp_path, system="dark")
        assert h.bridge.effectiveTheme == "light"
        h.bridge.themePreference = "system"
        assert h.bridge.effectiveTheme == "dark"
        assert load_settings(tmp_path).theme == "system"
        assert h.fired("preference") == 1
        assert h.fired("effective") == 1

    def test_same_preference_emits_nothing(self, tmp_path: Path) -> None:
        save_settings(Settings(theme="dark"), tmp_path)
        h = BridgeHarness(tmp_path, system="light")
        h.bridge.themePreference = "dark"
        assert h.bridge.themePreference == "dark"
        assert h.fired("preference") == 0
        assert h.fired("effective") == 0

    def test_preference_change_without_effective_change(self, tmp_path: Path) -> None:
        # "system" with a dark system already resolves to "dark"; switching to
        # the explicit "dark" changes the stored preference but not the theme.
        h = BridgeHarness(tmp_path, system="dark")
        h.bridge.themePreference = "dark"
        assert load_settings(tmp_path).theme == "dark"
        assert h.fired("preference") == 1
        assert h.fired("effective") == 0

    def test_invalid_preference_rejected_and_nothing_written(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        for bad in ("banana", "", "DARK", "Light", None):
            h.bridge.themePreference = bad  # type: ignore[arg-type]
            assert h.bridge.themePreference == "system"
            assert h.bridge.effectiveTheme == "dark"
            assert h.fired("preference") == 0
            assert h.fired("effective") == 0
            assert not (tmp_path / SETTINGS_FILENAME).exists()

    def test_persist_failure_applies_live_and_never_raises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Regression: save_theme's OSError (disk full / read-only) used to
        # propagate straight out of the QML-facing property setter.
        def boom(_preference, _data_dir=None):
            raise OSError("read-only volume")

        monkeypatch.setattr(bridge_mod, "save_theme", boom)
        h = BridgeHarness(tmp_path, system="dark")
        h.bridge.themePreference = "light"  # must not raise
        assert h.bridge.themePreference == "light"  # applied live
        assert h.bridge.effectiveTheme == "light"
        assert h.fired("preference") == 1
        assert not (tmp_path / SETTINGS_FILENAME).exists()  # nothing persisted


class TestRefreshSystemTheme:
    def test_refresh_picks_up_system_change(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path, system="dark")
        assert h.bridge.effectiveTheme == "dark"
        h.system_theme.value = "light"
        h.bridge.refreshSystemTheme()
        assert h.bridge.effectiveTheme == "light"
        assert h.fired("effective") == 1

    def test_refresh_explicit_preference_ignores_system(self, tmp_path: Path) -> None:
        save_settings(Settings(theme="light"), tmp_path)
        h = BridgeHarness(tmp_path, system="dark")
        h.system_theme.value = "light"  # OS flips, preference stays "light"
        h.bridge.refreshSystemTheme()
        assert h.bridge.effectiveTheme == "light"
        assert h.fired("effective") == 0

    def test_refresh_without_change_emits_nothing(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path, system="dark")
        h.bridge.refreshSystemTheme()
        assert h.bridge.effectiveTheme == "dark"
        assert h.fired("effective") == 0


class TestEngineNoteIsModelFree:
    def test_fake_detector_seam_precludes_engine_construction(self, tmp_path: Path) -> None:
        # The note comes from the injected callable only; the bridge never
        # builds a TTSEngine (NFR-2.1) — its module must not even name one.
        h = BridgeHarness(tmp_path)
        h.bridge.resolve_engine_note()
        assert h.detector.calls == 1
        assert h.bridge.engineNote == NOTE
        assert not hasattr(bridge_mod, "TTSEngine")

    def test_production_defaults_are_model_free(self, tmp_path: Path) -> None:
        # Real default detector (detect_hardware → capability note): cheap,
        # headless, and never touches a model file or the engine package.
        bridge = ShellBridge(settings_dir=tmp_path)
        bridge.resolve_engine_note()
        assert isinstance(bridge.engineNote, str)
        assert bridge.engineNote.strip()
        assert bridge.themePreference == "system"
        assert bridge.effectiveTheme in {"dark", "light"}


class TestEngineNoteDeferred:
    """Startup perf: construction must not probe hardware (torch import)."""

    def test_construction_leaves_note_pending_without_probing(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        assert h.detector.calls == 0
        assert h.bridge.engineNote == bridge_mod.ENGINE_NOTE_PENDING

    def test_resolve_emits_once_and_dedupes_same_note(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        fired: list[bool] = []
        h.bridge.engineNoteChanged.connect(lambda: fired.append(True))
        h.bridge.resolve_engine_note()
        assert h.bridge.engineNote == NOTE
        h.bridge.resolve_engine_note()  # same note again → no re-emission
        assert fired == [True]
        assert h.detector.calls == 2

    def test_async_resolve_marshals_result_back(
        self,
        tmp_path: Path,
        qcoreapp,  # type: ignore[valid-type]
    ) -> None:
        import time as _time

        bridge = ShellBridge(settings_dir=tmp_path, detector=RecordingDetector(NOTE))
        bridge.resolve_engine_note_async()
        deadline = _time.monotonic() + 5.0
        while bridge.engineNote == bridge_mod.ENGINE_NOTE_PENDING:
            assert _time.monotonic() < deadline, "probe result never landed"
            qcoreapp.processEvents()
            _time.sleep(0.01)
        assert bridge.engineNote == NOTE


class TestWindowGeometry:
    """Placement persistence: restore map at construction, save on close."""

    def test_fresh_settings_give_empty_geometry_map(self, tmp_path: Path) -> None:
        bridge = ShellBridge(settings_dir=tmp_path, detector=RecordingDetector())
        assert bridge.initialWindowGeometry == {}

    def test_saved_geometry_round_trips_through_settings(self, tmp_path: Path) -> None:
        bridge = ShellBridge(settings_dir=tmp_path, detector=RecordingDetector())
        bridge.saveWindowGeometry(120, 64, 1280, 800, True)
        reloaded = ShellBridge(settings_dir=tmp_path, detector=RecordingDetector())
        assert reloaded.initialWindowGeometry == {
            "x": 120,
            "y": 64,
            "width": 1280,
            "height": 800,
            "maximized": True,
        }

    def test_save_preserves_other_settings_fields(self, tmp_path: Path) -> None:
        save_settings(Settings(theme="dark"), tmp_path)
        bridge = ShellBridge(settings_dir=tmp_path, detector=RecordingDetector())
        bridge.saveWindowGeometry(0, 0, 1000, 600, False)
        assert load_settings(tmp_path).theme == "dark"

    def test_offscreen_placement_is_dropped(self, tmp_path: Path) -> None:
        from PySide6.QtCore import QRect

        class FakeScreen:
            def __init__(self, rect: QRect) -> None:
                self._rect = rect

            def availableGeometry(self) -> QRect:
                return self._rect

        screens = (FakeScreen(QRect(0, 0, 1440, 900)),)
        settings = Settings(window_x=5000, window_y=5000, window_width=1280, window_height=800)
        geo = bridge_mod._restorable_geometry(settings, screens_provider=lambda: screens)
        assert geo == {"width": 1280, "height": 800}  # off-screen x/y dropped
        onscreen = replace(settings, window_x=100, window_y=100)
        assert bridge_mod._restorable_geometry(onscreen, screens_provider=lambda: screens) == {
            "x": 100,
            "y": 100,
            "width": 1280,
            "height": 800,
        }

    def test_below_minimum_size_is_dropped(self, tmp_path: Path) -> None:
        settings = Settings(window_x=10, window_y=10, window_width=320, window_height=200)
        geo = bridge_mod._restorable_geometry(settings, screens_provider=lambda: ())
        assert geo == {"x": 10, "y": 10}
