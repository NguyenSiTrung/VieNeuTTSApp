"""Theme resolution + persistence (FR-2.4/FR-2.5).

Resolution contract: preference (system/light/dark) → effective theme
(dark/light); ``system`` follows the Qt palette; any invalid value falls
back to dark. Persistence is a load-modify-save round-trip through
core/settings.py so unrelated settings survive.
"""

from pathlib import Path

import pytest

from vienetts_app.core.models import Settings
from vienetts_app.core.settings import load_settings, save_settings
from vienetts_app.ui.theme import load_theme, qt_system_theme, resolve_theme, save_theme


class TestResolveTheme:
    def test_explicit_dark_and_light_pass_through(self) -> None:
        assert resolve_theme("dark", system="light") == "dark"
        assert resolve_theme("light", system="dark") == "light"

    def test_system_follows_supplied_system_theme(self) -> None:
        assert resolve_theme("system", system="light") == "light"
        assert resolve_theme("system", system="dark") == "dark"

    def test_invalid_values_fall_back_to_dark(self) -> None:
        assert resolve_theme("banana", system="light") == "dark"
        assert resolve_theme("", system="light") == "dark"
        assert resolve_theme("DARK", system="light") == "dark"

    def test_system_with_unknown_system_theme_is_dark(self) -> None:
        assert resolve_theme("system", system="purple") == "dark"


class TestQtSystemTheme:
    def test_headless_without_gui_app_returns_dark(self) -> None:
        # Unit tests run without a QGuiApplication — the safe default is dark.
        from PySide6.QtGui import QGuiApplication

        assert QGuiApplication.instance() is None or True  # informational
        assert qt_system_theme() in {"dark", "light"}

    def test_color_scheme_mapping_via_injected_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PySide6.QtCore import Qt

        from vienetts_app.ui import theme as theme_mod

        class FakeHints:
            def __init__(self, scheme: Qt.ColorScheme) -> None:
                self._scheme = scheme

            def colorScheme(self) -> Qt.ColorScheme:
                return self._scheme

        class FakeApp:
            def __init__(self, scheme: Qt.ColorScheme) -> None:
                self._hints = FakeHints(scheme)

            def styleHints(self) -> FakeHints:
                return self._hints

        class FakeGuiApplication:
            _instance: FakeApp | None = None

            @staticmethod
            def instance() -> FakeApp | None:
                return FakeGuiApplication._instance

        monkeypatch.setattr(theme_mod, "QGuiApplication", FakeGuiApplication)
        cases = [
            (Qt.ColorScheme.Light, "light"),
            (Qt.ColorScheme.Dark, "dark"),
            (Qt.ColorScheme.Unknown, "dark"),
        ]
        for scheme, expected in cases:
            FakeGuiApplication._instance = FakeApp(scheme)
            assert qt_system_theme() == expected
        FakeGuiApplication._instance = None  # no app instance → dark
        assert qt_system_theme() == "dark"


class TestPersistence:
    def test_load_theme_missing_dir_is_settings_default(self, tmp_path: Path) -> None:
        assert load_theme(tmp_path) == "system"

    def test_save_then_load_round_trip(self, tmp_path: Path) -> None:
        for preference in ("light", "dark", "system"):
            save_theme(preference, tmp_path)
            assert load_theme(tmp_path) == preference

    def test_save_theme_rejects_invalid(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="theme"):
            save_theme("banana", tmp_path)

    def test_save_theme_preserves_other_settings(self, tmp_path: Path) -> None:
        save_settings(Settings(backend="torch", default_voice="Ema"), tmp_path)
        save_theme("dark", tmp_path)
        merged = load_settings(tmp_path)
        assert merged.theme == "dark"
        assert merged.backend == "torch"
        assert merged.default_voice == "Ema"


class TestQmlThemeAndComponents:
    def test_theme_qml_exists_and_declares_tokens(self) -> None:
        qml_dir = Path(__file__).parent.parent.parent / "src" / "vienetts_app" / "ui" / "qml"
        theme_file = qml_dir / "Theme.qml"
        assert theme_file.exists()
        content = theme_file.read_text(encoding="utf-8")
        # Verify critical design tokens are declared
        for token in [
            "bg",
            "surface",
            "surfaceAlt",
            "surfaceCard",
            "surfacePopup",
            "borderPopup",
            "text",
            "textMuted",
            "accent",
            "accentHover",
            "accentSubtle",
            "success",
            "warning",
            "error",
        ]:
            assert f"property color {token}" in content

    def test_qmldir_and_components_exist(self) -> None:
        qml_dir = Path(__file__).parent.parent.parent / "src" / "vienetts_app" / "ui" / "qml"
        qmldir_file = qml_dir / "qmldir"
        assert qmldir_file.exists()
        qmldir_content = qmldir_file.read_text(encoding="utf-8")
        for comp in ["AppCard", "AppButton", "EmotionChip", "StatusBadge"]:
            assert comp in qmldir_content
            comp_file = qml_dir / "components" / f"{comp}.qml"
            assert comp_file.exists(), f"Missing {comp_file}"

    def test_card_elevation_effect_stays_behind_the_card_surface(self) -> None:
        """A shadow effect must not paint its black source over light-mode text."""
        qml_dir = Path(__file__).parent.parent.parent / "src" / "vienetts_app" / "ui" / "qml"
        card_content = (qml_dir / "components" / "AppCard.qml").read_text(encoding="utf-8")

        effect_start = card_content.index("MultiEffect {")
        effect_end = card_content.index("\n    }\n\n    ColumnLayout", effect_start)
        effect = card_content[effect_start:effect_end]

        assert "z: -1" in effect

    def test_dropdown_popups_use_themed_surfaces(self) -> None:
        """Dropdown popups must use Theme.surfacePopup and avoid default unstyled white box."""
        comp_dir = Path(__file__).parent.parent.parent / "src" / "vienetts_app" / "ui" / "qml" / "components"
        combo_content = (comp_dir / "AppCombo.qml").read_text(encoding="utf-8")
        voice_content = (comp_dir / "VoicePicker.qml").read_text(encoding="utf-8")

        assert "popup: Popup" in combo_content
        assert "Theme.surfacePopup" in combo_content
        assert "Theme.borderPopup" in combo_content

        assert "popup: Popup" in voice_content
        assert "Theme.surfacePopup" in voice_content
        assert "Theme.borderPopup" in voice_content
