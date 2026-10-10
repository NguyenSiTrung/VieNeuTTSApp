"""Theme resolution + persistence (FR-2.4/FR-2.5).

Resolution contract: preference (system/light/dark) → effective theme
(dark/light); ``system`` follows the Qt palette; any invalid value falls
back to dark. Persistence is a load-modify-save round-trip through
core/settings.py so unrelated settings survive.
"""

import re
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
        qmldir_content = (qml_dir / "qmldir").read_text(encoding="utf-8")
        for comp in [
            "AppCard",
            "AppButton",
            "AppSegmented",
            "EmotionChip",
            "StatusBadge",
            "StatusBar",
        ]:
            assert comp in qmldir_content
            assert (qml_dir / "components" / f"{comp}.qml").exists(), f"Missing {comp}"

        """Settings rows carry no decorative icon tile (audit FR-1.6).

        A tile is an AppIcon whose enclosing QML object is a Rectangle (the
        tinted square it sat in); status glyphs inside layouts are fine.
        """
        settings = (qml_dir / "SettingsTab.qml").read_text(encoding="utf-8")
        tiles = []
        for match in re.finditer(r"\bAppIcon \{", settings):
            depth, pos = 0, match.start()
            while pos > 0:  # walk back to the unmatched "{" that encloses it
                pos -= 1
                if settings[pos] == "}":
                    depth += 1
                elif settings[pos] == "{":
                    if depth == 0:
                        break
                    depth -= 1
            opener = settings[settings.rfind("\n", 0, pos) + 1 : pos].strip()
            if opener == "Rectangle":
                tiles.append(settings.count("\n", 0, match.start()) + 1)
        assert tiles == [], f"icon tiles at SettingsTab.qml lines {tiles}"

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

    def test_type_floor_hit_target_and_text_contrast(self) -> None:
        """Audit FR-1.1..1.3: 12 px type floor, 44 px targets, AA subtle text."""
        qml_dir = Path(__file__).parent.parent.parent / "src" / "vienetts_app" / "ui" / "qml"
        theme = (qml_dir / "Theme.qml").read_text(encoding="utf-8")

        def int_token(name: str) -> int:
            return int(re.search(rf"property int {name}: (\d+)", theme).group(1))

        assert int_token("fontSizeXs") >= 12
        assert int_token("controlHitTarget") >= 44

        def color_token(name: str) -> dict[str, str]:
            dark, light = re.search(
                rf'property color {name}: isDark \? "(#[0-9a-fA-F]{{6}})" : "(#[0-9a-fA-F]{{6}})"',
                theme,
            ).groups()
            return {"dark": dark, "light": light}

        def luminance(hex_color: str) -> float:
            channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(a: str, b: str) -> float:
            hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        for text in ("text", "textMuted", "textSubtle"):
            for surface in ("bg", "surface", "surfaceCard", "statusBarBg"):
                for mode in ("dark", "light"):
                    ratio = contrast(color_token(text)[mode], color_token(surface)[mode])
                    assert ratio >= 4.5, f"{text} on {surface} ({mode}) = {ratio:.2f}:1"
        # Status bar (FR-2.1): one step darker than bg in both themes, and its
        # warning copy stays AA on it.
        for mode in ("dark", "light"):
            assert luminance(color_token("statusBarBg")[mode]) < luminance(color_token("bg")[mode])
            ratio = contrast(color_token("warningText")[mode], color_token("statusBarBg")[mode])
            assert ratio >= 4.5, f"warningText on statusBarBg ({mode}) = {ratio:.2f}:1"

        # No rendered size below the floor: literal sizes or token arithmetic.
        offenders = []
        for path in qml_dir.rglob("*.qml"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                literal = re.search(r"pixelSize:\s*(\d+)", line)
                if (literal and int(literal.group(1)) < 12) or re.search(
                    r"pixelSize:\s*Theme\.fontSize\w+\s*-", line
                ):
                    offenders.append(f"{path.relative_to(qml_dir).as_posix()}:{lineno}")
        assert offenders == []


class TestStatusBarLogic:
    """StatusBarLogic.js — the status bar's pure readout rules (FR-2.1)."""

    def test_readout_never_empty_and_readiness_keys(self, qcoreapp) -> None:
        from PySide6.QtQml import QJSEngine

        path = (
            Path(__file__).parents[2]
            / "src"
            / "vienetts_app"
            / "ui"
            / "qml"
            / "components"
            / "StatusBarLogic.js"
        )
        source = path.read_text(encoding="utf-8")
        assert source.startswith(".pragma library\n")
        engine = QJSEngine()
        loaded = engine.evaluate(source.removeprefix(".pragma library\n"), str(path))
        assert not loaded.isError(), loaded.toString()

        def call(name: str, *args: object) -> object:
            result = (
                engine.globalObject().property(name).call([engine.toScriptValue(a) for a in args])
            )
            assert not result.isError(), result.toString()
            return result.toVariant()

        # Readout: the note once known, else the model state word, never ""/"…".
        assert call("readoutText", "ONNX Runtime CPU · int8", "Sẵn sàng") == (
            "ONNX Runtime CPU · int8"
        )
        for pending in ("", "…", "...", "  …  ", None):
            assert call("readoutText", pending, "Đang kiểm tra...") == "Đang kiểm tra..."
        assert call("readoutText", "…", "") not in ("", "…", "...")
        assert call("isMeaningful", " … ") is False
        assert call("isMeaningful", "CPU") is True

        # Readiness: VieNeu from its model state, managed from EngineState.
        vieneu = {
            "ready": "ready",
            "downloading": "busy",
            "validating": "busy",
            "failed": "failed",
            "unavailable": "missing",
            "checking": "checking",
            "": "checking",
        }
        for model_state, key in vieneu.items():
            assert call("readinessKey", model_state, False, "missing") == key
        for readiness in ("ready", "busy", "failed", "unsupported", "missing"):
            assert call("readinessKey", "ready", True, readiness) == readiness
        assert call("readinessKey", "ready", True, "checking") == "checking"
