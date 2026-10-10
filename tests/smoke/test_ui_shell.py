"""Offscreen UI shell smoke suite (AC-2, AC-3, AC-5; NFR-2.2).

Launches the real GUI assembly — create_app + ShellBridge + Main.qml — under
``QT_QPA_PLATFORM=offscreen`` and drives it exactly like the smoke criteria
require: window present, four tabs navigable via the bridge, live theme
switch folded into the persistence "restart" (a second bridge+window built
after a theme write, reading the same settings dir), plus the Phase 4
edge-case surfaces: models-missing overlay (FR-4.6c), export-only notice
(FR-4.6a), and the polished consent copy (FR-4.7).

Each scenario runs in its own subprocess: Qt allows exactly one
QGuiApplication per process, and pytest-qt's qapp fixture may leave a
headless QCoreApplication from the CLI tests (see track learnings — QML
aborts without a QGuiApplication). The subprocess script prints a
``RESULT:``-prefixed JSON line that these tests assert on.

Edge-case scenarios inject fakes ONLY at the controller's seams (an engine
factory raising the real ``ModelsMissingError`` marker message; an audio-
probe callable per FR-4.6a) while running the REAL controller, REAL worker
thread and REAL QML wiring — fake-at-the-seam per the project's pattern.

Tab-level audio gate (``audio_gate_tabs``, FR-4.6a): a forced-False probe on
the REAL controller keeps the export-only notice up and drives BOTH
synthesis tabs' playButton (plus the cloning preview) into export-only
posture (audio-ready state reached via a REAL batch job + quick export over
a success duck-typed engine); refreshAudioAvailability() after the probe
flips True clears the notice and re-enables playback everywhere.

Rendered-size scan (``type_scan_<W>x<H>``, AC-1 of ui_shell_redesign): every
destination is activated at the given window size and the driver's
``rendered_size_offenders`` walker reports visible text under 12 px and
visible AbstractButtons under 44 px; the test fails on any offender missing
from ``KNOWN_SIZE_OFFENDERS_1120X740`` (empty since Task 1.5). The same walk
checks the button hierarchy (FR-1.5): at most one visible ``primary``
AppButton per destination (and per shell overlay) in the idle state, and
every disabled filled AppButton painted with ``controlDisabledBg``.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from vienetts_app.ui.bridge import TABS

pytestmark = pytest.mark.smoke

DRIVER = textwrap.dedent(
    """\
    import gc
    import json
    import sys
    import time
    from pathlib import Path

    from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QPointF

    from vienetts_app.app import create_app
    from vienetts_app.ui.bg_ops import run_sync
    from vienetts_app.core.engine import (
        FETCH_MODELS_COMMAND,
        MODELS_MISSING_MARKER,
        ModelsMissingError,
    )
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.controller import AppController

    settings_root = sys.argv[1]
    scenarios = sys.argv[2].split(",")

    # This suite asserts Vietnamese UI copy; the app's "system" language
    # default follows the HOST locale (en_* hosts would render English and
    # break those assertions). Stub the controller's locale probe so every
    # scenario resolves the Vietnamese source language deterministically.
    import vienetts_app.ui.controller as _controller_module

    class _ViLocale:
        @staticmethod
        def system():
            return _ViLocale()

        def name(self):
            return "vi_VN"

    _controller_module.QLocale = _ViLocale

    # --- Rendered-size scan (AC-1: 12 px type floor, 44 px hit target) -----
    # ONE reusable walker for every window size / destination: it walks the
    # window's ROOT item tree (childItems, so Repeater delegates and open
    # popups under the overlay are reached), skips subtrees whose EFFECTIVE
    # visibility is false (non-current tabs, closed disclosures, hidden
    # notices), whose accumulated opacity is 0, or that sit under a clipping
    # ancestor of zero area. Scrolled-out content inside a ScrollView stays
    # in: below-the-fold controls are still real targets.
    import re

    from PySide6.QtGui import QFontInfo
    from PySide6.QtQuick import QQuickItem

    from vienetts_app.ui.bridge import TABS

    SCAN_TEXT_FLOOR = 12
    SCAN_TARGET_FLOOR = 44
    TAB_SCOPES = {tab_id + "Tab": tab_id for tab_id, _label in TABS}
    # QML-defined types register as ``AppButton_QMLTYPE_19`` (the number is
    # load-order dependent) — strip it so ids survive across runs.
    QML_SUFFIX = re.compile(r"(_QML(TYPE)?_[0-9]+)+$")

    def qml_type(item):
        return QML_SUFFIX.sub("", item.metaObject().className())

    def item_label(item):
        # Stable identity: objectName, else the QML type plus its visible
        # label (fakes are deterministic), else the bare type — never a
        # childItems() index (delegate order is arbitrary).
        if item.objectName():
            return item.objectName()
        model = item.property("modelData")  # Repeater delegate row
        if isinstance(model, dict):
            model = model.get("id") or model.get("name") or model.get("label")
        if isinstance(model, str) and model.strip():
            return qml_type(item) + "[" + model.strip()[:32].replace(":", ";") + "]"
        for prop in ("text", "title", "tooltipText", "accessibleLabel", "iconKind"):
            value = item.property(prop)
            if isinstance(value, str) and value.strip():
                text = " ".join(value.split())[:32].replace(":", ";")
                return qml_type(item) + '"' + text + '"'
        return qml_type(item)

    def font_px(item):
        font = item.property("font")
        if font is None:
            return None
        if font.pixelSize() > 0:
            return font.pixelSize()
        return QFontInfo(font).pixelSize()  # point-sized font: resolved px

    def rendered_size_offenders(window):
        \"\"\"Map offender id -> details for the window's current posture.

        Ids read ``<scope>:<named-ancestors>/<label>:<kind>:<measure>`` where
        scope is the tab id (item lives under ``<tab>Tab``) or ``shell``,
        kind is ``text`` (measure ``<px>px``) or ``target`` (measure ``w``,
        ``h`` or ``wh``: the dimension(s) under the 44 px floor).
        \"\"\"
        found = {}
        checked = {}
        stack = [(window.contentItem(), 1.0, "shell", ())]
        while stack:
            item, opacity, scope, chain = stack.pop()
            if not item.isVisible():
                continue
            opacity *= item.opacity()
            if opacity <= 0:
                continue
            name = item.objectName()
            if name in TAB_SCOPES:
                scope, chain = TAB_SCOPES[name], ()
            width, height = item.width(), item.height()
            sized = width > 0 and height > 0
            path = "/".join(chain[-2:] + (item_label(item),))
            key = None
            is_text = item.inherits("QQuickText")
            is_input = item.inherits("QQuickTextInput") or item.inherits("QQuickTextEdit")
            if sized and (is_text or is_input):
                text = item.property("text")
                if is_input or (isinstance(text, str) and text.strip()):
                    checked[scope] = checked.get(scope, 0) + 1
                    px = font_px(item)
                    if px is not None and px < SCAN_TEXT_FLOOR:
                        key = scope + ":" + path + ":text:" + str(px) + "px"
            if sized and item.inherits("QQuickAbstractButton"):
                checked[scope] = checked.get(scope, 0) + 1
                short = "".join(
                    dim
                    for dim, value in (("w", width), ("h", height))
                    if round(value) < SCAN_TARGET_FLOOR
                )
                if short:
                    key = scope + ":" + path + ":target:" + short
            if key is not None:
                entry = found.setdefault(
                    key, {"count": 0, "size": [round(width), round(height)]}
                )
                entry["count"] += 1
            if item.clip() and not sized:
                continue  # clipped away: nothing below can render
            if name[:1].islower() and name not in TAB_SCOPES:
                # App objectNames are camelCase; Qt's own auto-named
                # internals ("ApplicationWindow", "TextTab") are skipped.
                chain = chain + (name,)
            for child in item.childItems():
                stack.append((child, opacity, scope, chain))
        return found, checked

    def qml_types(item):
        # The QML type and every QML/C++ base (SubtitleCard -> AppCard -> ...).
        names, meta = [], item.metaObject()
        while meta is not None:
            names.append(QML_SUFFIX.sub("", meta.className()))
            meta = meta.superClass()
        return names

    def visible_items(root):
        stack = [root]
        while stack:
            item = stack.pop()
            if not item.isVisible() or item.opacity() <= 0:
                continue
            yield item
            stack.extend(item.childItems())

    def page_chrome(tab_item):
        \"\"\"Compact-chrome facts for one tab (FR-1.4).

        Returns visible PageHeader [label, height] pairs and the ids of any
        PageHeader/AppCard whose ``subtitle`` (deprecated, never rendered)
        still shows up as visible text inside it, plus how many subtitled
        headers/cards were checked (non-vacuity).
        \"\"\"
        headers, leaks, subtitled = [], [], 0
        for item in visible_items(tab_item):
            types = qml_types(item)
            if "PageHeader" in types:
                headers.append([item_label(item), round(item.height())])
            if "PageHeader" not in types and "AppCard" not in types:
                continue
            subtitle = item.property("subtitle")
            if not isinstance(subtitle, str) or not subtitle.strip():
                continue
            subtitled += 1
            wanted = " ".join(subtitle.split())
            for inner in visible_items(item):
                text = inner.property("text") if inner.inherits("QQuickText") else None
                if isinstance(text, str) and " ".join(text.split()) == wanted:
                    leaks.append(types[0] + ":" + item_label(item))
                    break
        return headers, leaks, subtitled

    def button_hierarchy(root, skip_name=""):
        \"\"\"Button-hierarchy facts for one screen state (FR-1.5).

        Returns the labels of visible ``variant: "primary"`` AppButtons and
        [label, variant, background colour] for every visible DISABLED
        filled AppButton (quiet/icon variants paint no background). The
        subtree named ``skip_name`` (the shell skips ``tabStack``) is left out.
        \"\"\"
        from PySide6.QtGui import QColor

        primaries, disabled = [], []
        stack = [root]
        while stack:
            item = stack.pop()
            if not item.isVisible() or item.opacity() <= 0:
                continue
            if skip_name and item.objectName() == skip_name:
                continue
            stack.extend(item.childItems())
            if "AppButton" not in qml_types(item):
                continue
            variant = str(item.property("variant"))
            if variant == "primary":
                primaries.append(item_label(item))
            if not bool(item.property("enabled")) and variant not in ("quiet", "ghost", "icon"):
                colour = QColor(item.property("buttonBgColor")).name()
                disabled.append([item_label(item), variant, colour])
        return primaries, disabled

    results = {}
    for scenario in scenarios:
        # Per-scenario settings workspace keeps groups isolated.
        settings_dir = str(Path(settings_root) / scenario)
        Path(settings_dir).mkdir(parents=True, exist_ok=True)
        out = {"scenario": scenario}

        # The app controller owns persisted language and output preferences. Keep
        # it in the scenario directory too, otherwise a developer's real settings
        # can install the English translator and invalidate Vietnamese copy pins.
        controller_factory = lambda: AppController(data_dir=Path(settings_dir))

        if scenario == "modelsmissing":

            class MissingWeightsEngine:
                \"\"\"Duck-typed engine whose lazy init hits the REAL marker path.\"\"\"

                sample_rate = 48_000
                backend = "onnx"

                def infer_stream(self, *args, **kwargs):
                    raise ModelsMissingError(
                        f"{MODELS_MISSING_MARKER}: the TTS model files were not "
                        f"found in the local Hugging Face cache (missing). Fetch "
                        f"the offline bundle once with `{FETCH_MODELS_COMMAND}`."
                    )
                    yield  # pragma: no cover - makes this a generator

                def close(self):
                    pass

            def controller_factory():
                return AppController(
                    data_dir=Path(settings_dir),
                    engine_factory=lambda **kw: MissingWeightsEngine(),
                    catalog=lambda: [],
                    saved_names=lambda voices_dir: [],
                )
        elif scenario == "audio_gate_tabs":
            import numpy as np

            audio_state = {"available": False}

            def audio_probe():
                return audio_state["available"]

            class ReadyEngine:
                \"\"\"Duck-typed engine whose batch infer succeeds immediately.\"\"\"

                sample_rate = 48_000
                backend = "onnx"

                def infer_stream(self, *args, **kwargs):
                    yield np.full(4800, 0.4, dtype=np.float32)

                def close(self):
                    pass

            def controller_factory():
                return AppController(
                    data_dir=Path(settings_dir),
                    engine_factory=lambda **kw: ReadyEngine(),
                    catalog=lambda: [],
                    saved_names=lambda voices_dir: [],
                    audio_probe=audio_probe,
                )
        elif scenario.startswith("type_scan_"):
            # Pin the export-only notice ON so the shell scan does not depend
            # on whether the host has an audio output device.
            def controller_factory():
                return AppController(data_dir=Path(settings_dir), audio_probe=lambda: False)
        elif scenario == "foreground":
            import threading

            import numpy as np

            gate = {"release": threading.Event()}

            class GatedEngine:
                \"\"\"Duck-typed engine whose batch infer blocks until released.\"\"\"

                sample_rate = 48_000
                backend = "onnx"

                def infer_stream(self, *args, **kwargs):
                    assert gate["release"].wait(timeout=15.0), "engine gate never released"
                    yield np.full(4800, 0.4, dtype=np.float32)

                def close(self):
                    pass

            def controller_factory():
                return AppController(
                    data_dir=Path(settings_dir),
                    engine_factory=lambda **kw: GatedEngine(),
                    catalog=lambda: [],
                    saved_names=lambda voices_dir: [],
                )

        def build():
            from vienetts_app.ui.audiobook_controller import AudiobookController
            from vienetts_app.ui.chapter_persist import SyncPersistExecutor

            return create_app(
                bridge_factory=lambda: ShellBridge(
                    settings_dir=settings_dir,
                    detector=lambda: "SMOKE NOTE",
                    system_theme=lambda: "light",
                ),
                controller_factory=controller_factory,
                # Keep the audiobook workspace inside the scenario tmp dir —
                # the default factory would touch the real user data dir.
                audiobook_factory=lambda controller: AudiobookController(
                    controller, data_dir=Path(settings_dir), bg_runner=run_sync,
                    persist_executor=SyncPersistExecutor(),
                ),
            )

        app, engine = build()
        window = engine.rootObjects()[0]

        def pump_until(cond, timeout=5.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cond():
                    return True
                app.processEvents()
                time.sleep(0.01)
            return False

        lazy_tabs = ("paragraph", "studio", "audiobook", "cloning", "settings")
        if scenario == "lazy_tabs":
            # Read BEFORE the event loop runs: only the landing tab is built,
            # and the idle prebuild must not have fired yet.
            loaders = {t: window.findChildren(QObject, t + "Loader") for t in lazy_tabs}
            out["loaders_found"] = all(len(found) == 1 for found in loaders.values())
            out["loaders_async"] = all(
                bool(found[0].property("asynchronous")) for found in loaders.values() if found
            )
            out["built_at_startup"] = sorted(
                t for t in lazy_tabs if window.findChildren(QObject, t + "Tab")
            )
            out["text_built_at_startup"] = bool(window.findChildren(QObject, "textTab"))
            out["prebuild_at_startup"] = bool(window.property("prebuildTabs"))
            first_frames = []
            window.frameSwapped.connect(
                lambda: first_frames.append(bool(window.property("prebuildTabs")))
            )

        # Tabs load asynchronously (idle prebuild after the first frame):
        # every scenario waits for each tab Loader to reach Loader.Ready.
        out["tabs_ready"] = pump_until(lambda: bool(window.property("tabsReady")), 20.0)

        if scenario == "lazy_tabs":
            out["prebuild_at_first_frame"] = first_frames[0] if first_frames else None
            out["built_after_idle"] = sorted(
                t for t in lazy_tabs if window.findChildren(QObject, t + "Tab")
            )
            # Each Loader exposes ``ready`` (status === Loader.Ready); the
            # Status enum itself has no Python converter.
            out["loaders_ready"] = all(
                bool(window.findChildren(QObject, t + "Loader")[0].property("ready"))
                for t in lazy_tabs
            )
        elif scenario == "nav_group":
            # Loader-deferred studios (oey): visit before the presence scan.
            nav_bridge = engine.rootContext().contextProperty("bridge")
            for tab_id in ("audiobook", "cloning", "settings"):
                nav_bridge.setCurrentTab(tab_id)
                app.processEvents()
            nav_bridge.setCurrentTab("text")
            tabs = [o.objectName() for o in window.findChildren(QObject)]
            out["window"] = window.objectName()
            out["tabs_present"] = all(
                n in tabs
                for n in ("textTab", "paragraphTab", "audiobookTab", "cloningTab", "settingsTab")
            )
            stack = window.findChildren(QObject, "tabStack")[0]
            visited = []
            for tab in ("text", "paragraph", "audiobook", "cloning", "settings"):
                bridge = engine.rootContext().contextProperty("bridge")
                bridge.setCurrentTab(tab)
                app.processEvents()
                # QML-declared property: read through the meta-object
                visited.append([tab, stack.property("currentIndex")])
            out["nav_visits"] = visited
            # Phase 1 Task 4: clean profile reports checking/unavailable, never
            # ready — the setup card (not a developer command) owns the state.
            setup = window.findChildren(QObject, "modelSetupOverlay")
            out["setup_found"] = len(setup) == 1
            out["setup_visible_default"] = bool(setup[0].property("visible")) if setup else False
            probe_controller = engine.rootContext().contextProperty("controller")
            out["model_state"] = str(probe_controller.property("modelState"))
            out["model_ready"] = bool(probe_controller.property("modelReady"))
            batch = engine.rootContext().contextProperty("batchController")
            out["batch_found"] = batch is not None
            out["batch_has_add_files"] = hasattr(batch, "addFiles")
            status_items = window.findChildren(QObject, "modelStatusText")
            out["status_found"] = len(status_items) == 1
            out["status_text"] = str(status_items[0].property("text")) if status_items else ""
            missing_cmd = window.findChildren(QObject, "modelsMissingCommand")
            out["no_developer_command"] = len(missing_cmd) == 0

            results["navigate"] = out
            out = {"scenario": "consentcopy"}
            cc_bridge = engine.rootContext().contextProperty("bridge")
            cc_bridge.setCurrentTab("cloning")  # Loader-deferred studio (oey)
            app.processEvents()
            labels = window.findChildren(QObject, "consentText")
            out["consent_found"] = len(labels) == 1
            out["consent_text"] = str(labels[0].property("text")) if labels else ""

            results["consentcopy"] = out
            out = {"scenario": "updatebadge"}
            # Real controller, no network: drive the badge through the same
            # property the silent startup/hourly check flips. The dot is a
            # visual-tree child of the Settings nav row (Repeater delegate).
            def item_walk(root):
                out, stack = [], [root]
                while stack:
                    cur = stack.pop()
                    out.append(cur)
                    for ch in cur.childItems():
                        stack.append(ch)
                return out

            def find_dots():
                return [
                    i
                    for i in item_walk(window.property("contentItem"))
                    if i.objectName() == "navUpdateDot"
                ]

            controller = engine.rootContext().contextProperty("controller")
            dots = find_dots()
            out["dot_found"] = len(dots) >= 1
            out["dot_hidden_initially"] = all(not bool(d.property("visible")) for d in dots)
            controller._update_available = True
            controller.updateAvailableChanged.emit()
            app.processEvents()
            dots = find_dots()
            out["dot_visible_after_check"] = any(bool(d.property("visible")) for d in dots)
            out["update_available"] = bool(controller.updateAvailable)

            results["updatebadge"] = out
            out = {"scenario": "card_shadows"}
            # Elevated cards carry an analytic RectangularShadow whose colour
            # is the theme's shadow token — read live in both themes.
            from PySide6.QtGui import QColor

            sh_bridge = engine.rootContext().contextProperty("bridge")

            def shadows():
                found = window.findChildren(QObject, "cardShadow")
                return [
                    {
                        "cls": shadow.metaObject().className(),
                        "visible": bool(shadow.property("visible")),
                        "color": QColor(shadow.property("color")).name(QColor.NameFormat.HexArgb),
                        "z": float(shadow.property("z")),
                    }
                    for shadow in found
                ]

            for theme in ("dark", "light"):
                sh_bridge.themePreference = theme
                pump_until(lambda: False, 0.3)  # let colour animations settle
                out[theme] = shadows()
            results["card_shadows"] = out

        elif scenario == "restart":
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
            bridge.themePreference = "light"
            app.processEvents()
            # "restart": fresh bridge + engine against the same settings dir
            app2, engine2 = build()
            bridge2 = engine2.rootContext().contextProperty("bridge")
            out["persisted_pref"] = bridge2.themePreference
            out["persisted_effective"] = bridge2.effectiveTheme
        elif scenario == "modelsmissing":
            controller = engine.rootContext().contextProperty("controller")
            out["initial_missing"] = bool(controller.modelsMissing)
            controller.generate("Xin chào", "Adam")
            # Real InferenceWorker thread: the terminal event is queued — pump until it lands.
            out["missing_after_error"] = pump_until(
                lambda: controller.modelsMissing and not controller.busy
            )
            overlays = window.findChildren(QObject, "modelSetupOverlay")
            out["overlay_found"] = len(overlays) == 1
            out["overlay_visible"] = bool(overlays[0].property("visible")) if overlays else False
            out["model_ready"] = bool(controller.property("modelReady"))
            out["model_state"] = str(controller.property("modelState"))
            status_items = window.findChildren(QObject, "modelStatusText")
            out["status_found"] = len(status_items) == 1
            out["status_text"] = str(status_items[0].property("text")) if status_items else ""
            missing_cmd = window.findChildren(QObject, "modelsMissingCommand")
            out["command_found"] = len(missing_cmd) == 0
            retry_buttons = window.findChildren(QObject, "modelRetryButton")
            out["retry_found"] = len(retry_buttons) == 1
            download_buttons = window.findChildren(QObject, "modelDownloadButton")
            out["download_found"] = len(download_buttons) == 1
            cancel_buttons = window.findChildren(QObject, "modelCancelButton")
            out["cancel_found"] = len(cancel_buttons) == 1
            # Cancel invokes the cooperative cancel slot without a dev command.
            if cancel_buttons:
                QMetaObject.invokeMethod(cancel_buttons[0], "click")
                app.processEvents()
            out["cancel_invoked"] = True
            out["flag_still_true"] = bool(controller.modelsMissing)
            controller.shutdown()  # stop the real worker thread before exit
        elif scenario == "audio_gate_tabs":
            window.setWidth(640)
            window.setHeight(740)
            window.show()
            for _ in range(10):
                app.processEvents()
                time.sleep(0.01)
            notice = window.findChildren(QObject, "exportOnlyNotice")[0]
            # Heavy studios are Loader-deferred (oey): visit each before the
            # narrow-layout scan asserts their subtrees. (Fresh bridge: the
            # loop variable from a previous scenario belongs to a torn-down
            # engine iteration.)
            bridge = engine.rootContext().contextProperty("bridge")
            for tab_id in ("audiobook", "cloning", "settings"):
                bridge.setCurrentTab(tab_id)
                app.processEvents()
            bridge.setCurrentTab("text")
            tabs = {
                name: window.findChildren(QObject, name)[0]
                for name in ("textTab", "paragraphTab", "audiobookTab", "cloningTab", "settingsTab")
            }
            out["notice_visible"] = bool(notice.property("visible"))
            out["notice_bottom"] = float(notice.y() + notice.height())
            out["tab_y"] = float(tabs["textTab"].mapToScene(QPointF(0, 0)).y())
            out["nav_width"] = float(window.findChildren(QObject, "navBar")[0].width())

            def tab_find(tab, name):
                (item,) = tab.findChildren(QObject, name)
                return item

            critical_items = {
                "text": ("voicePicker", "generateButton", "quickExportButton"),
                "paragraph": ("voicePicker", "generateButton", "exportButton"),
                "settings": (
                    "backendCombo",
                    "precisionCombo",
                    "defaultVoiceCombo",
                    "outputDirBrowseButton",
                    "temperatureSpin",
                ),
                "cloning": ("consentAcceptButton",),
            }
            bridge = engine.rootContext().contextProperty("bridge")
            out["window_width"] = float(window.width())
            out["tab_widths"] = {}
            out["critical_right_edges"] = {}
            for tab_name, names in critical_items.items():
                bridge.setCurrentTab(tab_name)
                app.processEvents()
                tab = tabs[tab_name + "Tab"]
                out["tab_widths"][tab_name] = float(tab.width())
                out["critical_right_edges"].update(
                    {
                        name: float(
                            tab_find(tab, name).mapToScene(
                                QPointF(tab_find(tab, name).width(), 0)
                            ).x()
                        )
                        for name in names
                    }
                )

            results["narrow_layout"] = out
            out = {"scenario": "audio_gate_tabs"}
            from pathlib import Path

            text_tab = window.findChildren(QObject, "textTab")[0]
            para_tab = window.findChildren(QObject, "paragraphTab")[0]

            def tab_find(tab, name):
                matches = tab.findChildren(QObject, name)
                assert len(matches) == 1, name
                return matches[0]

            controller = engine.rootContext().contextProperty("controller")
            text_play = tab_find(text_tab, "playButton")
            para_play = tab_find(para_tab, "playButton")
            text_quick = tab_find(text_tab, "quickExportButton")
            para_export = tab_find(para_tab, "exportButton")

            # Export-only posture while the probe is still False (FR-4.6a): the
            # notice is up and no playback surface is usable, but the shell
            # offers the quiet re-probe affordance instead of a dev command.
            app.processEvents()
            notices = window.findChildren(QObject, "exportOnlyNotice")
            out["notice_found"] = len(notices) == 1
            out["notice_visible_off"] = bool(notices[0].property("visible"))
            out["audio_available_off"] = bool(controller.audioAvailable)
            refresh_buttons = window.findChildren(QObject, "audioRefreshButton")
            out["refresh_variant"] = (
                refresh_buttons[0].property("variant") if refresh_buttons else ""
            )
            # Cloning studio is Loader-deferred: activate it first (oey).
            ec_bridge = engine.rootContext().contextProperty("bridge")
            ec_bridge.setCurrentTab("cloning")
            app.processEvents()
            cloning_tabs = window.findChildren(QObject, "cloningTab")
            previews = window.findChildren(QObject, "previewPlayButton")
            out["preview_found"] = len(previews) == 1
            if cloning_tabs and previews:
                # Select a clip so ONLY the audio gate can hold enabled=False
                # (QML function args travel as QVariant through the metaobject).
                QMetaObject.invokeMethod(
                    cloning_tabs[0], "selectClip", Q_ARG("QVariant", "/tmp/reference.wav")
                )
                app.processEvents()
                out["preview_enabled_off"] = bool(previews[0].property("enabled"))

            # Ready-minus-device state through REAL flows: a batch job on the
            # real worker thread (queued done signal), then a quick export that
            # writes an actual WAV. Only the audio gate can then hold playButton.
            controller.generate("Xin chào thế giới", "Adam")
            out["audio_ready"] = pump_until(
                lambda: controller.hasAudio and not controller.busy, timeout=15.0
            )
            controller.outputDir = settings_dir  # keep the export inside tmp
            QMetaObject.invokeMethod(text_quick, "click")
            out["export_path_set"] = pump_until(
                lambda: str(controller.lastExportPath) != "", timeout=5.0
            )
            out["wav_exists"] = Path(str(controller.lastExportPath)).is_file()

            # Probe False → export-only posture (FR-4.6a): exports usable on BOTH
            # tabs while every playback button is gated off — still gated even
            # though a ready artifact now exists (readiness ≠ device present).
            out["audio_available_off_after_ready"] = bool(controller.audioAvailable)
            out["text_export_enabled_off"] = bool(text_quick.property("enabled"))
            out["para_export_enabled_off"] = bool(para_export.property("enabled"))
            out["text_play_disabled_off"] = not bool(text_play.property("enabled"))
            out["para_play_disabled_off"] = not bool(para_play.property("enabled"))

            # Device hot-plug seam: probe flips True; refreshAudioAvailability()
            # re-probes and re-notifies → the notice clears and every playback
            # control (both tabs + the cloning preview) re-enables.
            audio_state["available"] = True
            controller.refreshAudioAvailability()
            app.processEvents()
            out["audio_available_on"] = bool(controller.audioAvailable)
            out["notice_visible_on"] = bool(notices[0].property("visible"))
            if previews:
                out["preview_enabled_on"] = bool(previews[0].property("enabled"))
            pump_until(
                lambda: bool(text_play.property("enabled"))
                and bool(para_play.property("enabled")),
                timeout=5.0,
            )
            app.processEvents()
            out["audio_available_after_refresh"] = bool(controller.audioAvailable)
            out["text_play_enabled_after_refresh"] = bool(text_play.property("enabled"))
            out["para_play_enabled_after_refresh"] = bool(para_play.property("enabled"))
            controller.shutdown()  # stop the real worker thread before exit
            results["audio_gate_tabs"] = out

        elif scenario == "foreground":
            controller = engine.rootContext().contextProperty("controller")
            text_tab = window.findChildren(QObject, "textTab")[0]

            def tab_find(tab, name):
                matches = tab.findChildren(QObject, name)
                assert len(matches) == 1, name
                return matches[0]

            # The foreground status/cancel is the busy row: `busy` is
            # foreground-scoped and flips together with foregroundJobState, so
            # it is the Text tab's ONE status line (a second state row would
            # render a duplicate Cancel button).
            status = tab_find(text_tab, "busyLabel")
            cancel_button = tab_find(text_tab, "cancelButton")

            def visible_huy_controls(tab):
                # Count visible controls labelled "Hủy" — any second cancel
                # control on this tab is a regression, whatever it is named.
                total = 0
                for item in tab.findChildren(QObject):
                    meta = item.metaObject()
                    if meta.indexOfProperty("text") < 0:
                        continue
                    if meta.indexOfProperty("variant") < 0:
                        continue
                    if str(item.property("text")) == "Hủy" and bool(
                        item.property("visible")
                    ):
                        total += 1
                return total

            out["idle_hidden"] = not bool(status.property("visible"))
            # Submit with the engine gate closed: the job cannot finish, so
            # the foreground state is observable. The state flip itself is
            # synchronous; no event pumping happens before reading it.
            controller.generate("Xin chào", "")
            out["state_after_submit"] = str(controller.foregroundJobState)
            app.processEvents()
            out["status_visible"] = bool(status.property("visible"))
            out["status_text"] = str(status.property("text"))
            out["cancel_enabled"] = bool(cancel_button.property("enabled"))
            out["visible_cancel_controls"] = visible_huy_controls(text_tab)
            # The controller synchronously requests cancellation. A fast worker
            # can also deliver its valid cancelled terminal state immediately.
            controller.cancel()
            out["cancel_requested_state"] = str(controller.foregroundJobState)
            app.processEvents()
            out["cancel_requested_text"] = str(status.property("text"))
            out["cancel_disabled_while_cancelling"] = not bool(
                cancel_button.property("enabled")
            )
            gate["release"].set()
            out["settled_after_worker_terminal"] = pump_until(
                lambda: not controller.busy, timeout=15.0
            )
            app.processEvents()
            out["hidden_after"] = not bool(status.property("visible"))
            controller.shutdown()  # stop the real worker thread before exit

        elif scenario.startswith("type_scan_"):
            # type_scan_<W>x<H>: activate every destination at that window
            # size and union the rendered-size offenders (AC-1).
            width, height = (int(v) for v in scenario.removeprefix("type_scan_").split("x"))
            window.setWidth(width)
            window.setHeight(height)
            frames = [0]
            window.frameSwapped.connect(lambda: frames.__setitem__(0, frames[0] + 1))
            scan_bridge = engine.rootContext().contextProperty("bridge")
            offenders = {}
            out["checked"] = {}
            out["tab_visible"] = {}
            out["headers"] = {}
            out["subtitle_leaks"] = {}
            out["subtitled_checked"] = 0
            out["primaries"] = {}
            out["disabled_buttons"] = []
            for tab_id, _label in TABS:
                scan_bridge.setCurrentTab(tab_id)
                seen = frames[0]
                # Two presented frames: layouts polish during the frame sync.
                pump_until(lambda: frames[0] >= seen + 2, 3.0)
                (tab_item,) = window.findChildren(QObject, tab_id + "Tab")
                out["tab_visible"][tab_id] = bool(tab_item.property("visible"))
                found, checked = rendered_size_offenders(window)
                out["checked"][tab_id] = checked.get(tab_id, 0)
                (typed_tab,) = window.findChildren(QQuickItem, tab_id + "Tab")
                headers, leaks, subtitled = page_chrome(typed_tab)
                out["headers"][tab_id] = headers
                out["subtitle_leaks"][tab_id] = leaks
                out["subtitled_checked"] += subtitled
                primaries, disabled = button_hierarchy(typed_tab)
                out["primaries"][tab_id] = primaries
                out["disabled_buttons"].extend([tab_id] + row for row in disabled)
                for key, info in found.items():
                    offenders.setdefault(key, info)
            out["window_size"] = [round(window.width()), round(window.height())]
            # Shell overlays (model setup screen, notices) are their own state.
            primaries, disabled = button_hierarchy(window.contentItem(), "tabStack")
            out["primaries"]["shell"] = primaries
            out["disabled_buttons"].extend(["shell"] + row for row in disabled)
            out["effective_theme"] = scan_bridge.effectiveTheme
            # Tested header objectNames keep resolving after the FR-1.4 rework.
            out["paragraph_header_found"] = len(
                window.findChildren(QQuickItem, "paragraphPageHeader")
            )
            out["offenders"] = offenders
            scan_bridge.setCurrentTab("text")

        results[scenario] = out
        # Deterministic engine teardown before the next scenario
        # reuses this process (one QGuiApplication per process).
        engine.deleteLater()
        window = None
        engine = None
        gc.collect()
        app.processEvents()

    print("RESULT:" + json.dumps(results))
    """
)


# Rendered-size scan allowlist (AC-1, ui_shell_redesign_20261010): ids of
# visible Text/TextInput/TextEdit below 12 px or AbstractButtons below 44 px
# that are tolerated at 1120x740. EMPTY since Task 1.5 — AC-1 holds in every
# destination; keep it empty (fix offenders, never list them). Id format:
# ``<tab|shell>:<named ancestors>/<label>:<text|target>:<px | dimension(s)>``.
KNOWN_SIZE_OFFENDERS_1120X740: frozenset[str] = frozenset()


def run_driver(tmp_path, scenarios: list[str]) -> dict[str, dict]:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, str(tmp_path), ",".join(scenarios)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


def _theme_tokens() -> dict[str, dict[str, str]]:
    import re
    from pathlib import Path

    theme = (
        Path(__file__).parents[2] / "src" / "vienetts_app" / "ui" / "qml" / "Theme.qml"
    ).read_text(encoding="utf-8")
    tokens = {}
    for name in ("shadowColor", "shadowSubtle", "controlDisabledBg"):
        dark, light = re.search(
            rf'property color {name}: isDark \? "(#\w+)" : "(#\w+)"', theme
        ).groups()
        tokens[name] = {"dark": dark.lower(), "light": light.lower()}
    return tokens


class TestShellSmoke:
    """One subprocess covers the whole shell: navigation, theme, badge, edges."""

    @pytest.mark.slow
    def test_shell_navigation_theme_badge_and_edge_surfaces(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            # nav_group = navigate + consentcopy + updatebadge + card_shadows on
            # ONE window; audio_gate_tabs = narrow_layout + the audio gate on
            # ONE window (each result keeps its own key).
            [
                "lazy_tabs",
                "nav_group",
                "restart",
                "audio_gate_tabs",
                "modelsmissing",
                "foreground",
                "type_scan_1120x740",
            ],
        )
        result = results["navigate"]
        assert result["window"] == "mainWindow"
        assert result["tabs_present"] is True
        # Clean profile: setup card owns the state, never a dev command.
        assert result["setup_found"] is True
        assert result["setup_visible_default"] is True
        assert result["model_ready"] is False
        assert result["model_state"] in ("checking", "unavailable")
        assert result["batch_found"] is True
        assert result["batch_has_add_files"] is True
        assert result["status_found"] is True
        assert "/" in result["status_text"]
        assert result["no_developer_command"] is True
        visits = result["nav_visits"]
        assert [v[0] for v in visits] == [
            "text",
            "paragraph",
            "audiobook",
            "cloning",
            "settings",
        ]
        indices = [v[1] for v in visits]
        assert indices == sorted(indices) or len(set(indices)) == 5

        # Live theme switch (dark → light, then OS flip under pref=system) in
        # the SAME bridge instance that the restart rebuild persists.
        result = results["restart"]
        assert result["after_dark"] == "dark"
        assert result["after_light"] == "light"
        assert result["system_dark_effective"] == "dark"
        assert result["persisted_pref"] == "light"
        assert result["persisted_effective"] == "light"

        result = results["narrow_layout"]
        assert result["notice_visible"] is True
        assert result["notice_bottom"] <= result["tab_y"]
        assert all(width >= 560 for width in result["tab_widths"].values())
        assert result["nav_width"] <= 80
        assert all(
            right <= result["window_width"] for right in result["critical_right_edges"].values()
        )

        result = results["updatebadge"]
        assert result["dot_found"] is True
        assert result["dot_hidden_initially"] is True
        assert result["update_available"] is True
        assert result["dot_visible_after_check"] is True

        # Phase 4 edge-case surfaces (FR-4.6a/c, FR-4.7) in the REAL shell.
        # A factory-injected engine raising the REAL marker message through
        # the REAL worker thread → controller → QML overlay.
        result = results["modelsmissing"]
        assert result["initial_missing"] is False
        assert result["missing_after_error"] is True  # queued signal processed
        assert result["overlay_found"] is True
        assert result["overlay_visible"] is True
        assert result["model_ready"] is False
        assert result["model_state"] in ("unavailable", "failed", "checking")
        assert result["status_found"] is True
        assert "/" in result["status_text"]
        assert result["command_found"] is True
        assert result["retry_found"] is True
        assert result["download_found"] is True
        assert result["cancel_found"] is True
        assert result["cancel_invoked"] is True
        assert result["flag_still_true"] is True

        # Export-only notice posture (FR-4.6a): the notice/refresh affordance and
        # the cloning preview gate are read while the probe is still False,
        # before the single hot-plug flip that clears them (see the gate block
        # below, which shares this scenario).
        result = results["audio_gate_tabs"]
        assert result["notice_found"] is True
        assert result["notice_visible_off"] is True
        assert result["audio_available_off"] is False
        assert result["refresh_variant"] == "quiet"
        assert result["preview_found"] is True
        assert result["preview_enabled_off"] is False
        assert result["audio_available_on"] is True
        assert result["notice_visible_on"] is False
        assert result["preview_enabled_on"] is True

        result = results["consentcopy"]
        assert result["consent_found"] is True
        text = result["consent_text"]
        assert "đồng ý của chính người được sao chép" in text
        assert "quyền sử dụng giọng nói" in text
        assert "trách nhiệm của bạn" in text
        assert "mạo danh" in text

        result = results["audio_gate_tabs"]
        assert result["audio_ready"] is True
        assert result["export_path_set"] is True
        assert result["wav_exists"] is True
        assert result["text_export_enabled_off"] is True
        assert result["para_export_enabled_off"] is True
        assert result["audio_available_off_after_ready"] is False
        assert result["text_play_disabled_off"] is True
        assert result["para_play_disabled_off"] is True
        assert result["audio_available_after_refresh"] is True
        assert result["text_play_enabled_after_refresh"] is True
        assert result["para_play_enabled_after_refresh"] is True

        result = results["foreground"]
        assert result["idle_hidden"] is True
        assert result["state_after_submit"] == "queued"
        assert result["status_visible"] is True
        assert result["status_text"] in ("Đang chờ xử lý…", "Đang tổng hợp…")
        assert result["cancel_enabled"] is True
        # Exactly ONE Cancel control while generating (the duplicate
        # foreground-job row that rendered a second one is gone).
        assert result["visible_cancel_controls"] == 1
        assert result["cancel_requested_state"] in ("cancel_requested", "cancelled")
        if result["cancel_requested_state"] == "cancel_requested":
            assert result["cancel_requested_text"] == "Đang hủy…"
            assert result["cancel_disabled_while_cancelling"] is True
        else:
            assert result["cancel_requested_state"] == "cancelled"
        assert result["settled_after_worker_terminal"] is True
        assert result["hidden_after"] is True

        # Only the landing tab is built before the first frame (perf track 6.1).
        result = results["lazy_tabs"]
        assert result["loaders_found"] is True
        assert result["loaders_async"] is True
        # Nothing but the Text tab is instantiated when create_app returns.
        assert result["text_built_at_startup"] is True
        assert result["built_at_startup"] == []
        assert result["prebuild_at_startup"] is False
        # The idle prebuild fires only AFTER the first frame was presented...
        assert result["prebuild_at_first_frame"] is False
        # ...and then builds every deferred tab without a visit.
        assert result["tabs_ready"] is True
        assert result["built_after_idle"] == sorted(
            ["paragraph", "studio", "audiobook", "cloning", "settings"]
        )
        assert result["loaders_ready"] is True

        # Cards use an analytic shadow bound to the theme tokens (perf 6.3).
        result = results["card_shadows"]
        tokens = _theme_tokens()
        for theme in ("dark", "light"):
            shadows = result[theme]
            assert shadows, f"no card shadows found in {theme}"
            assert all("RectangularShadow" in s["cls"] for s in shadows)
            assert all(s["z"] < 0 for s in shadows)
            visible = [s["color"] for s in shadows if s["visible"]]
            assert visible, f"no visible card shadow in {theme}"
            allowed = {tokens["shadowColor"][theme], tokens["shadowSubtle"][theme]}
            assert set(visible) <= allowed, (theme, set(visible), allowed)
        # The two themes really resolve to different shadow colours.
        assert {s["color"] for s in result["dark"]} != {s["color"] for s in result["light"]}

        # Rendered-size scan (AC-1): every destination activated at 1120x740.
        result = results["type_scan_1120x740"]
        assert result["tabs_ready"] is True
        assert result["window_size"] == [1120, 740]
        # Non-vacuous: each tab was current when scanned and had items checked.
        assert all(result["tab_visible"].values()), result["tab_visible"]
        assert all(count > 0 for count in result["checked"].values()), result["checked"]
        offenders = result["offenders"]
        print("rendered-size offenders @1120x740:")
        for key in sorted(offenders):
            print(f"  {key}  {offenders[key]}")
        new = sorted(set(offenders) - KNOWN_SIZE_OFFENDERS_1120X740)
        assert not new, "new rendered-size offenders (fix them, do not allowlist): " + str(
            {key: offenders[key] for key in new}
        )
        assert offenders == {}  # AC-1 met everywhere since Task 1.5

        # Button hierarchy (FR-1.5): one primary per screen state at most, and
        # disabled buttons of every filled variant grey out — never accent.
        print("primaries:", result["primaries"])
        assert set(result["primaries"]) == {tab_id for tab_id, _ in TABS} | {"shell"}
        assert all(len(found) <= 1 for found in result["primaries"].values()), result["primaries"]
        disabled_bg = _theme_tokens()["controlDisabledBg"][result["effective_theme"]]
        disabled = result["disabled_buttons"]
        assert any(row[2] == "primary" for row in disabled), disabled  # non-vacuous
        assert all(row[3] == disabled_bg for row in disabled), disabled

        # Compact chrome (FR-1.4): every destination opens with ONE single-row
        # PageHeader within the 56 px budget, and no deprecated header/card
        # subtitle is rendered anywhere.
        print("page headers:", result["headers"])
        assert all(len(headers) >= 1 for headers in result["headers"].values()), result["headers"]
        assert all(
            height <= 56 for headers in result["headers"].values() for _label, height in headers
        ), result["headers"]
        assert result["subtitled_checked"] > 0  # callers still pass subtitles
        assert all(not leaks for leaks in result["subtitle_leaks"].values()), result[
            "subtitle_leaks"
        ]
        assert result["paragraph_header_found"] == 1
