"""ShellBridge: QML-exposed shell state (FR-2.2/FR-2.5/FR-2.7).

The bridge is a plain QObject: properties are Python attributes plus
synchronous Signal emissions, so direct calls suffice — no event-loop
pumping, no cross-thread queuing. Fakes are injected for the settings dir,
the engine-note detector (proving no TTSEngine is ever built, NFR-2.1), and
the system-theme probe.
"""

from pathlib import Path

import pytest

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
    def test_default_tab_and_fresh_settings(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path, system="light")
        assert h.bridge.currentTab == "text"
        assert h.bridge.themePreference == "system"
        assert h.bridge.effectiveTheme == "light"
        assert h.fired("tab") == 0
        assert h.fired("preference") == 0
        assert h.fired("effective") == 0

    def test_system_dark_resolves_dark(self, tmp_path: Path) -> None:
        assert BridgeHarness(tmp_path, system="dark").bridge.effectiveTheme == "dark"

    def test_preference_loaded_from_settings_file(self, tmp_path: Path) -> None:
        save_settings(Settings(theme="dark"), tmp_path)
        h = BridgeHarness(tmp_path, system="light")
        assert h.bridge.themePreference == "dark"
        assert h.bridge.effectiveTheme == "dark"  # explicit beats system

    def test_engine_note_from_injected_detector(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path, note="PyTorch · CUDA 12.8 · batched")
        assert h.bridge.engineNote == "PyTorch · CUDA 12.8 · batched"
        assert h.detector.calls == 1  # probed once at construction, never again


class TestTabsApi:
    def test_tabs_pairs_in_nav_order(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        # Labels are Vietnamese (the app's primary language); ids stay ASCII
        # because they double as settings values (FR-2.3, ui_refine_20260828).
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

    def test_every_tab_id_is_selectable(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        for tab_id, _ in TABS:
            h.bridge.setCurrentTab(tab_id)
            assert h.bridge.currentTab == tab_id
        assert h.fired("tab") == len(TABS) - 1  # "text" is already current

    def test_refresh_tabs_emits_for_live_language_switch(self, tmp_path: Path) -> None:
        # tabs is re-emitted after a UI-language swap so the nav re-reads
        # self.tr under the new translator (live switch, no restart).
        h = BridgeHarness(tmp_path)
        fired = []
        h.bridge.tabsChanged.connect(lambda: fired.append(True))
        assert fired == []  # no spurious emission at connect time
        h.bridge.refreshTabs()
        assert fired == [True]
        assert h.bridge.tabs[0] == {"id": "text", "label": "Văn bản"}


class TestCurrentTab:
    def test_slot_switches_and_emits(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        h.bridge.setCurrentTab("settings")
        assert h.bridge.currentTab == "settings"
        assert h.fired("tab") == 1

    def test_property_assignment_switches(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        h.bridge.currentTab = "cloning"
        assert h.bridge.currentTab == "cloning"
        assert h.fired("tab") == 1

    def test_same_tab_emits_nothing(self, tmp_path: Path) -> None:
        h = BridgeHarness(tmp_path)
        h.bridge.setCurrentTab("text")
        assert h.bridge.currentTab == "text"
        assert h.fired("tab") == 0

    @pytest.mark.parametrize("bad", ["banana", "", "Text", "text ", None, 3])
    def test_invalid_tab_rejected(self, tmp_path: Path, bad: object) -> None:
        h = BridgeHarness(tmp_path)
        h.bridge.setCurrentTab(bad)  # type: ignore[arg-type]
        assert h.bridge.currentTab == "text"
        assert h.fired("tab") == 0


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

    @pytest.mark.parametrize("bad", ["banana", "", "DARK", "Light", None])
    def test_invalid_preference_rejected_and_nothing_written(
        self, tmp_path: Path, bad: object
    ) -> None:
        h = BridgeHarness(tmp_path)
        h.bridge.themePreference = bad  # type: ignore[arg-type]
        assert h.bridge.themePreference == "system"
        assert h.bridge.effectiveTheme == "dark"
        assert h.fired("preference") == 0
        assert h.fired("effective") == 0
        assert not (tmp_path / SETTINGS_FILENAME).exists()


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
        assert h.detector.calls == 1
        assert h.bridge.engineNote == NOTE
        assert not hasattr(bridge_mod, "TTSEngine")

    def test_production_defaults_are_model_free(self, tmp_path: Path) -> None:
        # Real default detector (detect_hardware → capability note): cheap,
        # headless, and never touches a model file or the engine package.
        bridge = ShellBridge(settings_dir=tmp_path)
        assert isinstance(bridge.engineNote, str)
        assert bridge.engineNote.strip()
        assert bridge.themePreference == "system"
        assert bridge.effectiveTheme in {"dark", "light"}
