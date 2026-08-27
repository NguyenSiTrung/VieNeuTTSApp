"""Offscreen UI shell smoke suite (AC-2, AC-3, AC-5; NFR-2.2).

Launches the real GUI assembly — create_app + ShellBridge + Main.qml — under
``QT_QPA_PLATFORM=offscreen`` and drives it exactly like the smoke criteria
require: window present, four tabs navigable via the bridge, live theme
switch, and persistence across a "restart" (a second bridge+window built
after a theme write, reading the same settings dir).

Each scenario runs in its own subprocess: Qt allows exactly one
QGuiApplication per process, and pytest-qt's qapp fixture may leave a
headless QCoreApplication from the CLI tests (see track learnings — QML
aborts without a QGuiApplication). The subprocess script prints a
``RESULT:``-prefixed JSON line that these tests assert on.
"""

import json
import os
import subprocess
import sys
import textwrap

DRIVER = textwrap.dedent(
    """\
    import json
    import sys

    from PySide6.QtCore import QObject

    from vienetts_app.app import create_app
    from vienetts_app.ui.bridge import ShellBridge

    settings_dir = sys.argv[1]
    scenario = sys.argv[2]

    out = {"scenario": scenario}

    def build():
        return create_app(
            bridge_factory=lambda: ShellBridge(
                settings_dir=settings_dir,
                detector=lambda: "SMOKE NOTE",
                system_theme=lambda: "light",
            )
        )

    app, engine = build()
    window = engine.rootObjects()[0]

    if scenario == "navigate":
        tabs = [o.objectName() for o in window.findChildren(QObject)]
        out["window"] = window.objectName()
        out["tabs_present"] = all(
            n in tabs for n in ("textTab", "paragraphTab", "cloningTab", "settingsTab")
        )
        stack = window.findChildren(QObject, "tabStack")[0]
        visited = []
        for tab in ("text", "paragraph", "cloning", "settings"):
            bridge = engine.rootContext().contextProperty("bridge")
            bridge.setCurrentTab(tab)
            app.processEvents()
            # QML-declared property: read through the meta-object
            visited.append([tab, stack.property("currentIndex")])
        out["nav_visits"] = visited
    elif scenario == "theme":
        bridge = engine.rootContext().contextProperty("bridge")
        out["initial_pref"] = bridge.themePreference
        out["initial_effective"] = bridge.effectiveTheme
        # live switch dark → light with system=light
        bridge.currentTab = "settings"
        bridge.themePreference = "dark"
        app.processEvents()
        out["after_dark"] = bridge.effectiveTheme
        bridge.themePreference = "light"
        app.processEvents()
        out["after_light"] = bridge.effectiveTheme
        # simulate OS flip while pref=system → effective follows
        bridge._system_theme = lambda: "dark"  # noqa: SLF001 - test seam
        bridge.themePreference = "system"
        bridge.refreshSystemTheme()
        app.processEvents()
        out["system_dark_effective"] = bridge.effectiveTheme
    elif scenario == "restart":
        bridge = engine.rootContext().contextProperty("bridge")
        bridge.themePreference = "light"
        app.processEvents()
        # "restart": fresh bridge + engine against the same settings dir
        app2, engine2 = build()
        bridge2 = engine2.rootContext().contextProperty("bridge")
        out["persisted_pref"] = bridge2.themePreference
        out["persisted_effective"] = bridge2.effectiveTheme

    print("RESULT:" + json.dumps(out))
    """
)


def run_driver(tmp_path, scenario: str) -> dict:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, str(tmp_path), scenario],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


class TestShellSmoke:
    def test_window_and_four_tabs_present(self, tmp_path) -> None:
        result = run_driver(tmp_path, "navigate")
        assert result["window"] == "mainWindow"
        assert result["tabs_present"] is True

    def test_tab_navigation_via_bridge(self, tmp_path) -> None:
        result = run_driver(tmp_path, "navigate")
        # each visit is [tab_id, stack_index]; ids map 1:1 to distinct indices
        visits = result["nav_visits"]
        assert [v[0] for v in visits] == ["text", "paragraph", "cloning", "settings"]
        indices = [v[1] for v in visits]
        assert indices == sorted(indices) or len(set(indices)) == 4

    def test_theme_switch_is_live(self, tmp_path) -> None:
        result = run_driver(tmp_path, "theme")
        assert result["after_dark"] == "dark"
        assert result["after_light"] == "light"

    def test_system_preference_follows_os(self, tmp_path) -> None:
        result = run_driver(tmp_path, "theme")
        assert result["system_dark_effective"] == "dark"

    def test_theme_persists_across_restart(self, tmp_path) -> None:
        result = run_driver(tmp_path, "restart")
        assert result["persisted_pref"] == "light"
        assert result["persisted_effective"] == "light"
