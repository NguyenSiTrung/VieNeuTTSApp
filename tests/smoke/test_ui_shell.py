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
a success duck-typed engine); clicking the status bar's "Kiểm tra lại" after
the probe flips True clears the notice and re-enables playback everywhere.

Status bar (``statusbar`` in nav_group, plus the narrow-layout fit at 640 px;
ui_shell_redesign FR-2.1): one full-width bar pinned under nav + content
carries readiness, the engine note (``engineReadout``, never "" or "…"), the
export-only notice with ``audioRefreshButton`` and the update link; no
floating pill and no sidebar engine card remain.

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
    # Screen states the scan visits: every destination, every Tạo giọng đọc
    # mode and every Giọng đọc view, as (key, tab id, sub-mode, page
    # objectName). Every create mode is the one CreateTab page; both voices
    # views are the one VoicesTab page (library, or the hosted cloning flow).
    SCAN_SCREENS = (
        ("create:compose", "create", "compose", "createTab"),
        ("create:document", "create", "document", "createTab"),
        ("create:files", "create", "files", "createTab"),
        ("create:subtitles", "create", "subtitles", "createTab"),
        ("audiobook", "audiobook", "", "audiobookTab"),
        ("voices:library", "voices", "library", "voicesTab"),
        ("voices:clone", "voices", "clone", "voicesTab"),
        ("studio", "studio", "", "studioTab"),
        ("settings", "settings", "", "settingsTab"),
    )
    assert {screen[1] for screen in SCAN_SCREENS} == {tab_id for tab_id, _ in TABS}
    # Offender scope = the page an item lives under ("create", ...).
    TAB_SCOPES = {page: page.removesuffix("Tab") for *_rest, page in SCAN_SCREENS}
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
                # internals ("ApplicationWindow", "CreateTab") are skipped.
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

    def live_toggles(tab_item):
        \"\"\"Reachable ``livePreviewToggle`` controls under one tab (FR-2.5).

        A toggle counts when it is effectively visible, or when it sits in a
        CLOSED popup/menu whose opener item is effectively visible (one click
        away). Returns the toggle objects so callers can read ``checked``.
        \"\"\"
        reachable = []
        for obj in tab_item.findChildren(QObject, "livePreviewToggle"):
            if isinstance(obj, QQuickItem) and obj.isVisible():
                reachable.append(obj)
                continue
            node, crossed_popup = obj.parent(), False
            while node is not None and node is not tab_item:
                if not isinstance(node, QQuickItem) and node.inherits("QQuickPopup"):
                    crossed_popup = True
                elif crossed_popup and isinstance(node, QQuickItem):
                    if node.isVisible():
                        reachable.append(obj)
                    break
                node = node.parent()
        return reachable

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

        # Deferred pages as (Loader objectName, built item objectName): the
        # create mode workspaces (document/files/subtitles) incubate inside
        # the eager CreateTab like every other tab.
        lazy_tabs = {
            "createModes": "createModeWorkspaces",
            "studio": "studioTab",
            "audiobook": "audiobookTab",
            "voices": "voicesTab",
            "settings": "settingsTab",
        }
        if scenario == "lazy_tabs":
            # Read BEFORE the event loop runs: only the landing tab is built,
            # and the idle prebuild must not have fired yet.
            loaders = {t: window.findChildren(QObject, t + "Loader") for t in lazy_tabs}
            out["loaders_found"] = all(len(found) == 1 for found in loaders.values())
            out["loaders_async"] = all(
                bool(found[0].property("asynchronous")) for found in loaders.values() if found
            )
            out["built_at_startup"] = sorted(
                t for t, built in lazy_tabs.items() if window.findChildren(QObject, built)
            )
            out["create_built_at_startup"] = bool(
                window.findChildren(QObject, "createTab")
                and window.findChildren(QObject, "textEditor")
            )
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
                t for t, built in lazy_tabs.items() if window.findChildren(QObject, built)
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
                n in tabs for n in ("createTab", "audiobookTab", "cloningTab", "settingsTab")
            )
            stack = window.findChildren(QObject, "tabStack")[0]
            bridge = engine.rootContext().contextProperty("bridge")
            visited = []
            for tab, _label in TABS:
                bridge.setCurrentTab(tab)
                app.processEvents()
                # QML-declared property: read through the meta-object
                visited.append([tab, stack.property("currentIndex")])
            out["nav_visits"] = visited

            # Legacy tab ids (FR-3.1 aliases) still land on their pages:
            # [alias, currentTab, sub-mode, page shown, create page mode].
            def shown_page():
                return [
                    name
                    for name in ("createTab", "voicesTab", "cloningTab")
                    if window.findChildren(QQuickItem, name)[0].isVisible()
                ]

            create_page = window.findChildren(QQuickItem, "createTab")[0]
            alias_visits = []
            for alias in ("paragraph", "cloning", "text", "paragraph"):
                bridge.setCurrentTab(alias)
                app.processEvents()
                sub = bridge.voicesView if alias == "cloning" else bridge.createMode
                alias_visits.append(
                    [alias, bridge.currentTab, sub, shown_page(), create_page.property("mode")]
                )

            # The mode switch (FR-3.2) is bound strictly to bridge.createMode:
            # the shell moves the segment and the shown workspace, and a
            # segment click writes the shell state. Segments are Repeater
            # delegates, so they are reached through the visual tree.
            def visual_items(root):
                found_items, stack = [], [root]
                while stack:
                    it = stack.pop()
                    found_items.append(it)
                    stack.extend(it.childItems())
                return found_items

            switch = create_page.findChildren(QQuickItem, "createModeSwitch")[0]
            segments = {
                it.objectName().removeprefix("createModeSwitch_"): it
                for it in visual_items(switch)
                if it.objectName().startswith("createModeSwitch_")
            }
            workspace_cards = {
                "compose": "composeEditorCard",
                "document": "documentEditorCard",
                "files": "batchQueueCard",
                "subtitles": "subtitleCard",
            }

            def shown_cards():
                return sorted(
                    mode
                    for mode, card in workspace_cards.items()
                    if create_page.findChildren(QQuickItem, card)[0].isVisible()
                )

            def dock_shown():
                return create_page.findChildren(QQuickItem, "createDock")[0].isVisible()

            mode_sync = []
            for create_mode in ("files", "subtitles", "document", "compose"):
                bridge.setCreateMode(create_mode)
                app.processEvents()
                mode_sync.append(
                    [
                        create_mode,
                        create_page.property("mode"),
                        str(switch.property("currentValue")),
                        shown_cards(),
                        dock_shown(),
                    ]
                )
            segment_clicks = []
            for create_mode in ("document", "subtitles", "files", "compose"):
                QMetaObject.invokeMethod(segments[create_mode], "click")
                app.processEvents()
                segment_clicks.append(
                    [
                        create_mode,
                        bridge.createMode,
                        str(switch.property("currentValue")),
                        shown_cards(),
                    ]
                )
            out["segment_keys"] = sorted(segments)
            out["segment_clicks"] = segment_clicks
            # The page-side seam writes the shell state too.
            QMetaObject.invokeMethod(create_page, "setMode", Q_ARG("QVariant", "files"))
            app.processEvents()
            mode_sync.append(["page:files", bridge.createMode, shown_page()])
            bridge.setCreateMode("compose")
            app.processEvents()
            # Exactly ONE transport dock serves every mode.
            out["create_docks"] = sum(
                1
                for it in visual_items(create_page)
                if "TransportDock" in it.metaObject().className()
            )
            # The emotion chips live in the compose editor's toolbar row.
            toolbar = create_page.findChildren(QQuickItem, "composeToolbar")[0]
            out["emotion_in_toolbar"] = bool(
                toolbar.findChildren(QQuickItem, "emotionToolbar")
            ) and any(
                "EmotionChip" in it.metaObject().className() for it in visual_items(toolbar)
            )
            # Every mode's tested objectNames resolve inside the create page.
            out["mode_names_missing"] = sorted(
                name
                for name in (
                    "textEditor", "textClearButton", "textMetricsLabel", "emotionNote",
                    "paragraphEditor", "importButton", "importDialog",
                    "paragraphClearButton", "charCountLabel", "batchQueueCard",
                    "addFilesButton", "batchFileList", "runAllButton", "subtitleCard",
                    "subtitleImportButton", "createDock", "voicePicker",
                    "generateButton",
                )
                if not create_page.findChildren(QObject, name)
            )
            out["alias_visits"] = alias_visits
            out["mode_sync"] = mode_sync
            bridge.setCurrentTab("text")
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
            # The status bar carries the same fact as a link (FR-2.1).
            status_links = window.findChildren(QObject, "statusUpdateButton")
            out["status_link_found"] = len(status_links) == 1
            out["status_link_hidden_initially"] = all(
                not bool(b.property("visible")) for b in status_links
            )
            controller._update_available = True
            controller.updateAvailableChanged.emit()
            app.processEvents()
            dots = find_dots()
            out["dot_visible_after_check"] = any(bool(d.property("visible")) for d in dots)
            out["update_available"] = bool(controller.updateAvailable)
            out["status_link_visible_after_check"] = any(
                bool(b.property("visible")) for b in status_links
            )
            ub_bridge = engine.rootContext().contextProperty("bridge")
            out["tab_before_link"] = ub_bridge.currentTab
            if status_links:
                QMetaObject.invokeMethod(status_links[0], "click")
                app.processEvents()
            out["tab_after_link"] = ub_bridge.currentTab

            results["updatebadge"] = out
            out = {"scenario": "statusbar"}
            # FR-2.1: ONE status surface pinned under nav + content. The moved
            # objectNames live only inside it, the sidebar engine card is
            # gone, and the readout never reads "" or "…" in any model state.
            from vienetts_app.core.model_manager import ModelStatus

            sb_bridge = engine.rootContext().contextProperty("bridge")
            (bar,) = window.findChildren(QQuickItem, "statusBar")

            def inside_bar(item):
                cur = item.parentItem()
                while cur is not None:
                    if cur.objectName() == "statusBar":
                        return True
                    cur = cur.parentItem()
                return False

            corner = bar.mapToScene(QPointF(0, 0))
            out["bar"] = [corner.x(), corner.y(), bar.width(), bar.height()]
            out["window_size"] = [window.width(), window.height()]
            out["moved"] = {
                name: [
                    len(window.findChildren(QQuickItem, name)),
                    all(inside_bar(i) for i in window.findChildren(QQuickItem, name)),
                ]
                for name in (
                    "engineReadout",
                    "exportOnlyNotice",
                    "audioRefreshButton",
                    "statusUpdateButton",
                )
            }
            (nav,) = window.findChildren(QQuickItem, "navBar")
            nav_texts = [
                str(i.property("text")) for i in item_walk(nav) if i.inherits("QQuickText")
            ]
            out["nav_engine_card"] = [t for t in nav_texts if "Engine" in t or "NOTE" in t]
            (readout,) = window.findChildren(QQuickItem, "engineReadout")
            (state_label,) = window.findChildren(QQuickItem, "statusModelState")

            def readout_snapshot():
                app.processEvents()
                return {
                    "readout": str(readout.property("text")),
                    "readout_visible": readout.isVisible(),
                    "state_text": str(state_label.property("text")),
                    "state_visible": state_label.isVisible(),
                    "readiness": str(bar.property("readiness")),
                }

            original_status = controller._model_status
            out["note_pending"] = str(sb_bridge.engineNote)
            states = {str(controller.modelState): readout_snapshot()}
            for model_state in ("unavailable", "ready"):
                controller._publish_model_status(ModelStatus(state=model_state))
                states[model_state] = readout_snapshot()
            sb_bridge.resolve_engine_note()
            states["ready+note"] = readout_snapshot()
            controller._publish_model_status(original_status)
            app.processEvents()
            out["states"] = states

            results["statusbar"] = out
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
                for name in ("createTab", "audiobookTab", "cloningTab", "settingsTab")
            }
            out["notice_visible"] = bool(notice.property("visible"))
            # FR-2.1: the status bar spans the window bottom at full width,
            # below (never over) the tab content and the nav rail.
            (bar,) = window.findChildren(QQuickItem, "statusBar")
            corner = bar.mapToScene(QPointF(0, 0))
            out["bar"] = [corner.x(), corner.y(), bar.width(), bar.height()]
            out["window_height"] = float(window.height())
            create_tab = tabs["createTab"]
            out["tab_bottom"] = float(create_tab.mapToScene(QPointF(0, create_tab.height())).y())
            nav_bar = window.findChildren(QQuickItem, "navBar")[0]
            out["nav_bottom"] = float(nav_bar.mapToScene(QPointF(0, nav_bar.height())).y())
            out["nav_width"] = float(window.findChildren(QObject, "navBar")[0].width())
            # Fit at 640 px with every group competing: a long engine note,
            # the audio warning and the update link. The note drops first and
            # nothing overflows the bar.
            nb_controller = engine.rootContext().contextProperty("controller")
            bridge._apply_engine_note("ONNX Runtime CPU · CPU · fastest available engine here")
            nb_controller._update_available = True
            nb_controller.updateAvailableChanged.emit()
            for _ in range(5):
                app.processEvents()
            bar_right = bar.mapToScene(QPointF(bar.width(), 0)).x()
            out["bar_overflow"] = [
                item_label(i)
                for i in visible_items(bar)
                if i.width() > 0 and i.mapToScene(QPointF(i.width(), 0)).x() > bar_right + 0.5
            ]
            out["narrow_groups"] = {
                name: window.findChildren(QQuickItem, name)[0].isVisible()
                for name in (
                    "statusModelState",
                    "engineReadout",
                    "exportOnlyNotice",
                    "audioRefreshButton",
                    "statusUpdateButton",
                )
            }
            out["narrow_readout"] = str(
                window.findChildren(QQuickItem, "engineReadout")[0].property("text")
            )

            def tab_find(tab, name):
                (item,) = tab.findChildren(QObject, name)
                return item

            # (tab id to visit, page objectName, critical names). The create
            # page is checked in compose (via the "text" alias) and document.
            critical_items = {
                "text": ("createTab", ("voicePicker", "generateButton", "quickExportButton")),
                "paragraph": (
                    "createTab",
                    ("voicePicker", "generateButton", "exportButton", "importButton"),
                ),
                "settings": (
                    "settingsTab",
                    (
                        "backendCombo",
                        "precisionCombo",
                        "settingsDefaultVoiceLink",
                        "outputDirBrowseButton",
                        "temperatureSpin",
                    ),
                ),
                "cloning": ("cloningTab", ("consentAcceptButton",)),
            }
            bridge = engine.rootContext().contextProperty("bridge")
            out["window_width"] = float(window.width())
            out["tab_widths"] = {}
            out["critical_right_edges"] = {}
            frames = [0]
            window.frameSwapped.connect(lambda: frames.__setitem__(0, frames[0] + 1))
            for tab_name, (page_name, names) in critical_items.items():
                bridge.setCurrentTab(tab_name)
                seen = frames[0]
                # Two presented frames: a tab first shown here is laid out at
                # its first polish (the dock's wrapping Flow reads its
                # unconstrained one-row geometry until then).
                pump_until(lambda: frames[0] >= seen + 2, 3.0)
                tab = tabs[page_name]
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

            # 640×420 (the smallest supported window): the create page's
            # pinned dock goes compact (hint lines shed) and the header drops
            # its title row, so the editor keeps a usable visible height
            # between the toolbar and the dock.
            window.setHeight(420)
            bridge.setCurrentTab("text")
            seen = frames[0]
            pump_until(lambda: frames[0] >= seen + 2, 3.0)
            create_tab = tabs["createTab"]
            dock = tab_find(create_tab, "createDock")
            editor = tab_find(create_tab, "textEditor")
            editor_top = editor.mapToScene(QPointF(0, 0)).y()
            header = tab_find(create_tab, "createHeader")
            out["short_header"] = {
                "title_visible": bool(tab_find(create_tab, "createPageHeader").isVisible()),
                "right": float(header.mapToScene(QPointF(header.width(), 0)).x()),
                "switch_right": float(
                    tab_find(create_tab, "createModeSwitch")
                    .mapToScene(QPointF(tab_find(create_tab, "createModeSwitch").width(), 0))
                    .x()
                ),
                "import_right": float(
                    tab_find(create_tab, "importButton")
                    .mapToScene(QPointF(tab_find(create_tab, "importButton").width(), 0))
                    .x()
                ),
            }
            out["short_text_dock"] = {
                "compact": bool(dock.property("compact")),
                "hint_visible": bool(tab_find(create_tab, "createActionHint").isVisible()),
                "generate_visible": bool(tab_find(create_tab, "generateButton").isVisible()),
                "editor_visible_height": dock.mapToScene(QPointF(0, 0)).y() - editor_top,
                "dock_bottom": dock.mapToScene(QPointF(0, dock.height())).y(),
                "status_top": window.findChildren(QQuickItem, "statusBar")[0]
                .mapToScene(QPointF(0, 0))
                .y(),
            }
            # Same contract in the document mode (FR-2.3): the SAME dock,
            # compact, above the status bar, the document editor keeping
            # >= 80 px visible above it.
            bridge.setCurrentTab("paragraph")
            seen = frames[0]
            pump_until(lambda: frames[0] >= seen + 2, 3.0)
            pdock = tab_find(create_tab, "createDock")
            peditor = tab_find(create_tab, "paragraphEditor")
            out["short_paragraph_dock"] = {
                "compact": bool(pdock.property("compact")),
                "hint_visible": bool(tab_find(create_tab, "createActionHint").isVisible()),
                "generate_visible": bool(tab_find(create_tab, "generateButton").isVisible()),
                "editor_visible_height": pdock.mapToScene(QPointF(0, 0)).y()
                - peditor.mapToScene(QPointF(0, 0)).y(),
                "dock_bottom": pdock.mapToScene(QPointF(0, pdock.height())).y(),
                "status_top": window.findChildren(QQuickItem, "statusBar")[0]
                .mapToScene(QPointF(0, 0))
                .y(),
            }
            window.setHeight(740)
            app.processEvents()
            results["narrow_layout"] = out
            out = {"scenario": "audio_gate_tabs"}
            from pathlib import Path

            create_tab = window.findChildren(QObject, "createTab")[0]

            def tab_find(tab, name):
                matches = tab.findChildren(QObject, name)
                assert len(matches) == 1, name
                return matches[0]

            # One dock serves the compose ("text") and document ("para")
            # modes: the same controls are read in each mode below.
            controller = engine.rootContext().contextProperty("controller")
            ec_bridge = engine.rootContext().contextProperty("bridge")
            text_play = para_play = tab_find(create_tab, "playButton")
            text_quick = tab_find(create_tab, "quickExportButton")
            para_export = tab_find(create_tab, "exportButton")

            # Export-only posture while the probe is still False (FR-4.6a): the
            # notice is up and no playback surface is usable, but the shell
            # offers the quiet re-probe affordance instead of a dev command.
            app.processEvents()
            notices = window.findChildren(QObject, "exportOnlyNotice")
            out["notice_found"] = len(notices) == 1
            out["notice_visible_off"] = bool(notices[0].property("visible"))
            out["audio_available_off"] = bool(controller.audioAvailable)
            refresh_buttons = window.findChildren(QObject, "audioRefreshButton")
            out["refresh_found"] = len(refresh_buttons) == 1
            out["refresh_variant"] = (
                refresh_buttons[0].property("variant") if refresh_buttons else ""
            )
            out["refresh_visible_off"] = bool(
                refresh_buttons and refresh_buttons[0].property("visible")
            )
            # Cloning studio is Loader-deferred: activate it first (oey).
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
            ec_bridge.setCurrentTab("text")
            app.processEvents()
            out["text_export_enabled_off"] = bool(text_quick.property("enabled"))
            out["text_play_disabled_off"] = not bool(text_play.property("enabled"))
            ec_bridge.setCurrentTab("paragraph")
            app.processEvents()
            out["para_export_enabled_off"] = bool(para_export.property("enabled"))
            out["para_play_disabled_off"] = not bool(para_play.property("enabled"))

            # Device hot-plug seam: probe flips True; refreshAudioAvailability()
            # re-probes and re-notifies → the notice clears and every playback
            # control (both tabs + the cloning preview) re-enables.
            # The status bar's "Kiểm tra lại" is the re-probe (FR-2.1).
            audio_state["available"] = True
            QMetaObject.invokeMethod(refresh_buttons[0], "click")
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
            out["para_play_enabled_after_refresh"] = bool(para_play.property("enabled"))
            ec_bridge.setCurrentTab("text")
            app.processEvents()
            out["text_play_enabled_after_refresh"] = bool(text_play.property("enabled"))
            controller.shutdown()  # stop the real worker thread before exit
            results["audio_gate_tabs"] = out

        elif scenario == "foreground":
            controller = engine.rootContext().contextProperty("controller")
            text_tab = window.findChildren(QObject, "createTab")[0]

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

            def visible_stop_controls(tab):
                # Visible controls labelled as a stop/cancel ("Dừng" is the
                # TransportDock's Stop, FR-2.3; "Hủy" the pre-dock label) —
                # any second one on this tab is a regression, whatever it is
                # named. (Phát only reads "Dừng" while a replay runs.)
                found = []
                for item in tab.findChildren(QObject):
                    meta = item.metaObject()
                    if meta.indexOfProperty("text") < 0:
                        continue
                    if meta.indexOfProperty("variant") < 0:
                        continue
                    if str(item.property("text")) in ("Hủy", "Dừng") and bool(
                        item.property("visible")
                    ):
                        found.append(item.objectName())
                return found

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
            out["visible_cancel_controls"] = visible_stop_controls(text_tab)
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
            out["live_toggles"] = {}
            out["live_owner"] = {}
            live_by_screen = {}
            for screen, tab_id, sub, page in SCAN_SCREENS:
                if tab_id == "create":
                    scan_bridge.setCreateMode(sub)
                elif tab_id == "voices":
                    scan_bridge.setVoicesView(sub)
                scan_bridge.setCurrentTab(tab_id)
                seen = frames[0]
                # Two presented frames: layouts polish during the frame sync.
                pump_until(lambda: frames[0] >= seen + 2, 3.0)
                (tab_item,) = window.findChildren(QObject, page)
                out["tab_visible"][screen] = bool(tab_item.property("visible"))
                found, checked = rendered_size_offenders(window)
                out["checked"][screen] = checked.get(TAB_SCOPES[page], 0)
                (typed_tab,) = window.findChildren(QQuickItem, page)
                headers, leaks, subtitled = page_chrome(typed_tab)
                out["headers"][screen] = headers
                out["subtitle_leaks"][screen] = leaks
                out["subtitled_checked"] += subtitled
                primaries, disabled = button_hierarchy(typed_tab)
                out["primaries"][screen] = primaries
                out["disabled_buttons"].extend([screen] + row for row in disabled)
                # Live playback (FR-2.5): one reachable toggle per screen —
                # each create mode is its own screen state.
                toggles = live_toggles(typed_tab)
                out["live_toggles"][screen] = len(toggles)
                owners = []
                for toggle in toggles:
                    node = toggle.parent()
                    while node is not None and node.objectName() not in (
                        "createInspector", "settingsTab"
                    ):
                        node = node.parent()
                    owners.append(node.objectName() if node is not None else "")
                out["live_owner"][screen] = owners
                live_by_screen[screen] = toggles
                for key, info in found.items():
                    offenders.setdefault(key, info)
            (create_item,) = window.findChildren(QQuickItem, "createTab")
            out["create_mode_seen"] = create_item.property("mode")
            out["window_size"] = [round(window.width()), round(window.height())]
            # Shell overlays (model setup screen, notices) are their own state.
            primaries, disabled = button_hierarchy(window.contentItem(), "tabStack")
            out["primaries"]["shell"] = primaries
            out["disabled_buttons"].extend(["shell"] + row for row in disabled)
            out["effective_theme"] = scan_bridge.effectiveTheme
            # Tested header objectNames keep resolving after the FR-1.4 rework.
            out["create_header_found"] = len(window.findChildren(QQuickItem, "createPageHeader"))
            out["offenders"] = offenders

            # All live toggles are bound to the ONE global
            # controller.livePreview (flip it, every toggle follows).
            scan_controller = engine.rootContext().contextProperty("controller")
            live_before = bool(scan_controller.property("livePreview"))
            out["live_follow"] = {}
            for flipped in (not live_before, live_before):
                scan_controller.setProperty("livePreview", flipped)
                app.processEvents()
                for screen, toggles in live_by_screen.items():
                    out["live_follow"].setdefault(screen, []).extend(
                        bool(t.property("checked")) == flipped for t in toggles
                    )
            scan_bridge.setCurrentTab("text")
            scan_bridge.setVoicesView("library")

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

# Screen states the type_scan walk visits (keys of the driver's SCAN_SCREENS):
# the five destinations with Tạo giọng đọc's modes and Giọng đọc's views.
SCAN_SCREEN_KEYS = (
    "create:compose",
    "create:document",
    "create:files",
    "create:subtitles",
    "audiobook",
    "voices:library",
    "voices:clone",
    "studio",
    "settings",
)


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
        # Five destinations (FR-3.1), each on its own stack page.
        visits = result["nav_visits"]
        assert [v[0] for v in visits] == ["create", "audiobook", "voices", "studio", "settings"]
        assert len({v[1] for v in visits}) == 5, visits
        # Legacy ids land on the right destination/mode and page (AC-4).
        assert result["alias_visits"] == [
            ["paragraph", "create", "document", ["createTab"], "document"],
            ["cloning", "voices", "clone", ["voicesTab", "cloningTab"], "document"],
            ["text", "create", "compose", ["createTab"], "compose"],
            ["paragraph", "create", "document", ["createTab"], "document"],
        ]
        # CreateTab (FR-3.2): bridge.createMode drives the page mode, the
        # segmented switch and the ONE shown workspace; the dock hides only
        # for subtitles (SubtitleCard owns its transport).
        assert result["mode_sync"] == [
            ["files", "files", "files", ["files"], True],
            ["subtitles", "subtitles", "subtitles", ["subtitles"], False],
            ["document", "document", "document", ["document"], True],
            ["compose", "compose", "compose", ["compose"], True],
            ["page:files", "files", ["createTab"]],
        ]
        # ...and a segment click writes bridge.createMode back.
        assert result["segment_keys"] == ["compose", "document", "files", "subtitles"]
        assert result["segment_clicks"] == [
            [mode, mode, mode, [mode]] for mode in ("document", "subtitles", "files", "compose")
        ]
        assert result["create_docks"] == 1
        assert result["emotion_in_toolbar"] is True
        assert result["mode_names_missing"] == []

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
        # The status bar is pinned to the window bottom at full width and the
        # tab content / nav rail end above it (no floating pill over content).
        bar_x, bar_y, bar_w, bar_h = result["bar"]
        assert bar_x == 0 and bar_w == result["window_width"]
        assert bar_y + bar_h == result["window_height"]
        assert result["tab_bottom"] <= bar_y
        assert result["nav_bottom"] <= bar_y
        # 640 px with note + audio warning + update link: no overflow, the
        # engine note is the group that dropped, the rest stays reachable.
        assert result["bar_overflow"] == []
        assert result["narrow_groups"] == {
            "statusModelState": True,
            "engineReadout": False,
            "exportOnlyNotice": True,
            "audioRefreshButton": True,
            "statusUpdateButton": True,
        }
        assert result["narrow_readout"].startswith("ONNX Runtime CPU")
        assert all(width >= 560 for width in result["tab_widths"].values())
        assert result["nav_width"] <= 80
        assert all(
            right <= result["window_width"] for right in result["critical_right_edges"].values()
        )
        # 640×420: compact dock, still above the status bar, and the editor
        # keeps ≥ 80 px (about three lines) visible above it.
        short = result["short_text_dock"]
        assert short["compact"] is True
        assert short["hint_visible"] is False
        assert short["generate_visible"] is True
        assert short["dock_bottom"] <= short["status_top"]
        assert short["editor_visible_height"] >= 80, short
        # The header drops its title row at 640×420 and never overflows.
        header = result["short_header"]
        assert header["title_visible"] is False
        assert header["switch_right"] <= header["right"] + 0.5, header
        assert header["import_right"] <= header["right"] + 0.5, header
        assert header["right"] <= result["window_width"], header
        short = result["short_paragraph_dock"]
        assert short["compact"] is True
        assert short["hint_visible"] is False
        assert short["generate_visible"] is True
        assert short["dock_bottom"] <= short["status_top"]
        assert short["editor_visible_height"] >= 80, short

        result = results["updatebadge"]
        assert result["dot_found"] is True
        assert result["dot_hidden_initially"] is True
        assert result["update_available"] is True
        assert result["dot_visible_after_check"] is True
        assert result["status_link_found"] is True
        assert result["status_link_hidden_initially"] is True
        assert result["status_link_visible_after_check"] is True
        assert result["tab_before_link"] != "settings"
        assert result["tab_after_link"] == "settings"

        # FR-2.1 / AC-2: one status surface. The bar spans the window bottom;
        # the moved names exist once, inside it; the sidebar card is gone.
        result = results["statusbar"]
        bar_x, bar_y, bar_w, bar_h = result["bar"]
        win_w, win_h = result["window_size"]
        assert bar_x == 0 and bar_w == win_w
        assert bar_y + bar_h == win_h
        assert 34 <= bar_h <= 48
        assert result["moved"] == {
            "engineReadout": [1, True],
            "exportOnlyNotice": [1, True],
            "audioRefreshButton": [1, True],
            "statusUpdateButton": [1, True],
        }
        assert result["nav_engine_card"] == []
        # The readout falls back to the model state word while the engine
        # note is still pending, in every model state; never "" or "…".
        assert result["note_pending"] == "…"
        states = result["states"]
        expected = {
            "checking": ("checking", "Đang kiểm tra..."),
            "unavailable": ("missing", "Chưa có mô hình"),
            "ready": ("ready", "Sẵn sàng"),
        }
        assert set(states) >= {"unavailable", "ready", "ready+note"}
        for model_state, snap in states.items():
            assert snap["readout"].strip() not in ("", "…", "..."), (model_state, snap)
            assert snap["readout_visible"] is True, (model_state, snap)
            if model_state in expected:
                readiness, word = expected[model_state]
                assert snap["readiness"] == readiness, (model_state, snap)
                assert snap["readout"] == word, (model_state, snap)
                assert snap["state_visible"] is False  # no duplicate word
        # Once the detector lands, the readout is the engine note and the
        # state word sits beside the dot.
        assert states["ready+note"]["readout"] == "SMOKE NOTE"
        assert states["ready+note"]["state_text"] == "Sẵn sàng"
        assert states["ready+note"]["state_visible"] is True

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
        assert result["refresh_found"] is True
        assert result["refresh_variant"] == "quiet"
        assert result["refresh_visible_off"] is True
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
        # Exactly ONE stop control while generating — the dock's Dừng, which
        # replaced Tạo âm thanh in place (the duplicate foreground-job row
        # that rendered a second one is gone).
        assert result["visible_cancel_controls"] == ["cancelButton"]
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
        # Nothing but the create page's compose workspace is instantiated
        # when create_app returns.
        assert result["create_built_at_startup"] is True
        assert result["built_at_startup"] == []
        assert result["prebuild_at_startup"] is False
        # The idle prebuild fires only AFTER the first frame was presented...
        assert result["prebuild_at_first_frame"] is False
        # ...and then builds every deferred tab without a visit.
        assert result["tabs_ready"] is True
        assert result["built_after_idle"] == sorted(
            ["createModes", "studio", "audiobook", "voices", "settings"]
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
        assert set(result["primaries"]) == set(SCAN_SCREEN_KEYS) | {"shell"}
        assert all(len(found) <= 1 for found in result["primaries"].values()), result["primaries"]
        # Giọng đọc (Task 3.5): the library's one action is "Tạo giọng mới".
        assert result["primaries"]["voices:library"] == ["voicesCreateButton"]
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
        assert result["create_header_found"] == 1

        # Live playback (FR-2.5): at most one reachable toggle per screen —
        # the Create inspector in the compose and document modes (Phase 3:
        # it left the dock's overflow menu), the global preference row on
        # Settings — each following controller.livePreview. Batch files
        # render silently and subtitles hide the inspector: none there.
        print("live toggles:", result["live_toggles"])
        live = result["live_toggles"]
        assert all(count <= 1 for count in live.values()), live
        assert live["create:compose"] == 1 and live["create:document"] == 1, live
        assert live["create:files"] == 0 and live["create:subtitles"] == 0, live
        assert live["settings"] == 1, live
        assert live["voices:library"] == live["voices:clone"] == 0, live
        owner = result["live_owner"]
        assert owner["create:compose"] == owner["create:document"] == ["createInspector"]
        assert owner["settings"] == ["settingsTab"]
        # Every screen state the scan walks (all destinations + sub-modes).
        assert set(result["tab_visible"]) == set(SCAN_SCREEN_KEYS)
        assert {key.split(":")[0] for key in SCAN_SCREEN_KEYS} == {tab_id for tab_id, _ in TABS}
        assert result["create_mode_seen"] == "subtitles"  # create:subtitles reached the page
        follow = result["live_follow"]
        assert all(all(flags) for flags in follow.values()), follow
        # Non-vacuous: three reachable toggles (compose, document, settings),
        # each checked after both flips.
        assert sum(len(flags) for flags in follow.values()) >= 6
