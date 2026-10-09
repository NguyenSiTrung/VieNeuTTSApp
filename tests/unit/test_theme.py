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
    @pytest.mark.parametrize(
        ("preference", "system", "expected"),
        [
            ("dark", "light", "dark"),
            ("light", "dark", "light"),
            ("system", "light", "light"),
            ("system", "dark", "dark"),
            ("banana", "light", "dark"),
            ("", "light", "dark"),
            ("DARK", "light", "dark"),
            ("system", "purple", "dark"),
        ],
    )
    def test_resolve_theme(self, preference: str, system: str, expected: str) -> None:
        assert resolve_theme(preference, system=system) == expected


class TestQtSystemTheme:
    def test_system_theme_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Unit tests run without a QGuiApplication — the safe default is dark.
        from PySide6.QtGui import QGuiApplication

        assert QGuiApplication.instance() is None or True  # informational
        assert qt_system_theme() in {"dark", "light"}

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
    def test_persistence_contract(self, tmp_path: Path) -> None:
        assert load_theme(tmp_path) == "system"

        save_settings(Settings(backend="torch", default_voice="Ema"), tmp_path)
        for preference in ("light", "dark", "system"):
            save_theme(preference, tmp_path)
            assert load_theme(tmp_path) == preference
        merged = load_settings(tmp_path)
        assert merged.theme == "system"
        assert merged.backend == "torch"
        assert merged.default_voice == "Ema"


class TestQmlThemeAndComponents:
    def test_qml_theme_contract(self) -> None:
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
        qmldir_file = qml_dir / "qmldir"
        assert qmldir_file.exists()
        qmldir_content = qmldir_file.read_text(encoding="utf-8")
        for comp in ["AppCard", "AppButton", "EmotionChip", "StatusBadge"]:
            assert comp in qmldir_content
            comp_file = qml_dir / "components" / f"{comp}.qml"
            assert comp_file.exists(), f"Missing {comp_file}"

        """Card elevation is an analytic RectangularShadow, never a blur pass.

        The old MultiEffect rendered a hidden black shape through an offscreen
        blur per card; RectangularShadow draws the same soft edge in one
        shader with no source texture. It sits under the card surface (z: -1)
        and takes both theme shadow tokens.
        """
        qml_dir = Path(__file__).parent.parent.parent / "src" / "vienetts_app" / "ui" / "qml"
        card_content = (qml_dir / "components" / "AppCard.qml").read_text(encoding="utf-8")

        assert "MultiEffect" not in card_content
        shadow_start = card_content.index("RectangularShadow {")
        shadow = card_content[shadow_start : card_content.index("\n    }\n", shadow_start)]
        assert "z: -1" in shadow
        assert "Theme.shadowColor" in shadow
        assert "Theme.shadowSubtle" in shadow

        """No Canvas renders into a per-item framebuffer object (icons used one each)."""
        offenders = [
            path.relative_to(qml_dir).as_posix()
            for path in qml_dir.rglob("*.qml")
            if "FramebufferObject" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []

        """Dropdown popups must use Theme.surfacePopup and avoid default unstyled white box."""
        qml_dir = Path(__file__).parent.parent.parent / "src" / "vienetts_app" / "ui" / "qml"
        comp_dir = qml_dir / "components"
        combo_content = (comp_dir / "AppCombo.qml").read_text(encoding="utf-8")
        voice_content = (comp_dir / "VoicePicker.qml").read_text(encoding="utf-8")

        assert "popup: Popup" in combo_content
        assert "Theme.surfacePopup" in combo_content
        assert "Theme.borderPopup" in combo_content

        assert "popup: Popup" in voice_content
        assert "Theme.surfacePopup" in voice_content
        assert "Theme.borderPopup" in voice_content
