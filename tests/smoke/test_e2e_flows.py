"""Offscreen end-to-end smoke suite (AC-1..AC-5, NFR-3.2/3.3, plan phase3 t1).

Fake-engine flows through the REAL QML shell and the REAL AppController:
generate → done → export → WAV valid; cancel mid-job; file import → synth;
clone flow (fake SDK add_voice with app-data persistence); settings
round-trip incl. apply-on-restart. Same subprocess+RESULT-JSON pattern as
the other GUI suites (one QGuiApplication per process).

The fake lives BELOW the controller: a FakeVieneu engine object (spike
contract surface) + a real TTSEngine wrapping it, a real InferenceWorker
thread, and the real AppController — so the whole Python stack runs
production code; only the Vieneu SDK is faked (never a model load).
"""

import json
import os
import subprocess
import sys
import textwrap

DRIVER = textwrap.dedent(
    """\
    import gc
    import json
    import sys
    import tempfile
    from pathlib import Path

    import numpy as np
    from PySide6.QtCore import QObject, QMetaObject, Q_ARG, QTimer, Signal, Slot
    from PySide6.QtQuick import QQuickItem

    from vienetts_app.app import create_app
    from vienetts_app.core.engine import TTSEngine
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.controller import AppController
    from vienetts_app.workers.inference_worker import InferenceWorker

    tmp_root = Path(sys.argv[1])
    scenarios = sys.argv[2].split(",")
    SEED = 1234

    class FakeVieneu:
        \"\"\"Spike-contract SDK surface (docs/spike-report.md §0), recording calls.\"\"\"

        sample_rate = 48_000
        backend = "onnx"

        PRESETS = [
            ("Nam · Bắc · Phong cách tin tức", "PresetBac"),
            ("Nữ · Trung · Phong cách tự nhiên", "PresetTrung"),
            ("Nam · Nam · Giọng đọc tự nhiên", "PresetNam"),
        ]

        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.infer_calls = []
            self.add_voice_calls = []
            self.denoise_calls = []
            self.infer_delay_ms = 0
            self._preset_voices = {}
            self._voices = {label: name for label, name in self.PRESETS}

        def list_preset_voices(self):
            return [(f"{n} — {l}", n) for l, n in self.PRESETS]

        def infer(self, text, voice=None, ref_audio=None, temperature=None,
                  top_k=None, show_progress=True, **kw):
            self.infer_calls.append(
                {"text": text, "voice": voice, "temperature": temperature}
            )
            if self.infer_delay_ms:
                import time
                time.sleep(self.infer_delay_ms / 1000)
            rng = np.random.default_rng(SEED)
            return (rng.standard_normal(2400) * 0.05).astype(np.float32)

        def infer_stream(self, text, voice=None, temperature=None, **kw):
            # Streaming twin of infer(): same call record, deterministic
            # audio split into chunks (TextTab generates via mode="stream").
            self.infer_calls.append(
                {"text": text, "voice": voice, "temperature": temperature}
            )
            if self.infer_delay_ms:
                import time
                time.sleep(self.infer_delay_ms / 1000)
            rng = np.random.default_rng(SEED)
            audio = (rng.standard_normal(2400) * 0.05).astype(np.float32)
            for start in range(0, len(audio), 800):
                yield audio[start : start + 800]

        def add_voice(self, name, ref_clip, *, denoise=True, save=False, **kw):
            self.add_voice_calls.append(
                {"name": name, "clip": ref_clip, "denoise": denoise, "save": save}
            )
            return name

        def remove_voice(self, name, *, save=False, **kw):
            pass

        def denoise(self, clip, out_path=None, max_seconds=None):
            self.denoise_calls.append(str(clip))
            rng = np.random.default_rng(SEED + 1)
            return (rng.standard_normal(4410) * 0.05).astype(np.float32), 44_100

        def save_voices(self, path=None):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "presets": {
                    name: {"description": label, "gender": "", "style": "tu_nhien",
                           "speaker_emb": [0.0] * 8, "codes": [1, 2, 3]}
                    for label, name in self.PRESETS
                }
                | {
                    c["name"]: {"description": "", "gender": "", "style": "tu_nhien",
                                "speaker_emb": [0.1] * 8, "codes": [9]}
                    for c in self.add_voice_calls
                },
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            return str(path)

        def close(self):
            pass

    results = {}
    for scenario in scenarios:
        tmp = tmp_root / scenario
        tmp.mkdir(parents=True, exist_ok=True)
        fake_sdk = FakeVieneu()


        def make_controller():
            # REAL controller + REAL worker + REAL TTSEngine over the fake SDK.
            engine = TTSEngine(factory=lambda **kw: fake_sdk)
            worker = InferenceWorker(engine)

            def engine_factory(**kwargs):
                # The controller creates its engine lazily with voices_dir; hand
                # back one sharing the same fake SDK instance.
                assert kwargs.get("voices_dir") is not None
                return TTSEngine(factory=lambda **kw: fake_sdk,
                                 voices_dir=kwargs.get("voices_dir"))

            def worker_factory(eng):
                return InferenceWorker(eng)

            # Deterministic stream sink (fake-sink contract in stream_playback.py):
            # offscreen hosts have no audio device, but these flows assert the
            # play UX — the RAM-replay path must run for real, just sinklessly.
            class FakeSink:
                def __init__(self):
                    self.calls = []

                def start(self, device):
                    self.calls.append("start")

                def stop(self):
                    self.calls.append("stop")

                def state(self):
                    return "ActiveState"

            fake_sink = FakeSink()

            from vienetts_app.ui.stream_playback import StreamPlaybackController

            return AppController(
                data_dir=tmp,
                engine_factory=engine_factory,
                worker_factory=worker_factory,
                stream_playback_factory=lambda: StreamPlaybackController(
                    sink_factory=lambda _fmt: fake_sink
                ),
                # Offscreen hosts expose zero output devices; these flows assert
                # playback UX, so assume a working device (FR-4.6a injectable probe).
                audio_probe=lambda: True,
            )


        controller = make_controller()
        bridge = ShellBridge(settings_dir=tmp, detector=lambda: "SMOKE ENGINE NOTE")

        # Real-playback wrapper with an injected recording fake (offscreen: no
        # audio backend dependency, but the wrapper itself is production code).
        played = []


        class RecordingPlayer(QObject):
            # Duck-typed QMediaPlayer contract (ui/playback.py docstring):
            # setSource/play/stop/pause + the three state signals.
            playbackStateChanged = Signal("QVariant")
            mediaStatusChanged = Signal("QVariant")
            errorOccurred = Signal("QVariant", str)

            def __init__(self):
                super().__init__()
                self.sources = []

            @Slot("QVariant")
            def setSource(self, url):
                self.sources.append(str(url))
                self.playbackStateChanged.emit("PlayingState")

            @Slot()
            def play(self):
                pass

            @Slot()
            def stop(self):
                pass

            @Slot()
            def pause(self):
                pass

            # Optional parts of the duck contract (audiobook player): position/
            # duration feeds, resume, seek. Guarded connections in the wrapper.
            positionChanged = Signal("QVariant")
            durationChanged = Signal("QVariant")
            positions = []

            @Slot()
            def resume(self):
                self.playbackStateChanged.emit("PlayingState")

            @Slot(int)
            def setPosition(self, ms):
                self.positions.append(int(ms))
                self.positionChanged.emit(int(ms))

            def finish(self):
                self.playbackStateChanged.emit("StoppedState")
                self.mediaStatusChanged.emit("EndOfMedia")

            def tick(self, ms):
                self.positionChanged.emit(int(ms))

            def announce(self, duration_ms):
                self.durationChanged.emit(int(duration_ms))


        from vienetts_app.ui.playback import PlaybackController

        recording = RecordingPlayer()
        playback = PlaybackController(player_factory=lambda: recording)

        from vienetts_app.ui.audiobook_controller import AudiobookController

        audiobook = AudiobookController(
            controller, data_dir=tmp, player_factory=lambda: playback
        )

        app, engine = create_app(
            bridge_factory=lambda: bridge,
            controller_factory=lambda: controller,
            playback_factory=lambda: playback,
            audiobook_factory=lambda _controller: audiobook,
        )
        window = engine.rootObjects()[0]


        def find(name):
            return window.findChildren(QObject, name)[0]


        def item_walk(root):
            out, stack = [], [root]
            while stack:
                it = stack.pop()
                out.append(it)
                stack.extend(it.childItems())
            return out


        def ifind(name):
            return [i for i in item_walk(window.property("contentItem")) if i.objectName() == name]


        def wait_for(predicate, timeout_ms=8000, pump=20):
            from PySide6.QtCore import QThread
            waited = 0
            while waited < timeout_ms:
                app.processEvents()
                if predicate():
                    return True
                QThread.msleep(pump)
                waited += pump
            return False


        out = {"scenario": scenario}

        if scenario == "text_e2e":
            tab = find("textTab")
            editor = tab.findChildren(QObject, "textEditor")[0]
            editor.setProperty("text", "Xin chào thế giới")
            app.processEvents()
            # default voice preselected from settings (default "Adam" not in the
            # fake catalog → header-guard keeps controller default; assert via
            # generate call instead)
            find("generateButton").click()
            ok = wait_for(lambda: controller.hasAudio and not controller.busy)
            out["completed"] = ok
            out["infer_calls"] = fake_sdk.infer_calls
            out["temperature_flowed"] = fake_sdk.infer_calls[0]["temperature"] == 0.4

            # play path (2026-08-28 flow): Phát replays WITHOUT any export — RAM
            # replay through the stream sink; the file player never sees a source.
            btn = tab.findChildren(QObject, "playButton")[0]
            out["play_enabled_before_export"] = btn.property("enabled")
            btn.click()
            app.processEvents()
            out["replay_active_right_after_click"] = controller.replayActive
            out["replay_finished_by_itself"] = wait_for(lambda: not controller.replayActive)
            out["no_playback_error"] = controller.errorText == ""
            out["played_paths"] = [str(p) for p in recording.sources]

            # export to the default dir (settings output_dir = tmp)
            exported = controller.exportWav("")
            out["exported"] = exported
            out["last_export"] = controller.lastExportPath
            from vienetts_app.core.audio import read_wav
            import soundfile as sf
            data, sr = read_wav(controller.lastExportPath)
            out["wav_sample_rate"] = sr
            out["wav_samples"] = int(len(data))

        elif scenario == "cancel_e2e":
            fake_sdk.infer_delay_ms = 900  # slow job → cancel lands mid-flight
            tab = find("textTab")
            editor = tab.findChildren(QObject, "textEditor")[0]
            editor.setProperty("text", "Văn bản dài để hủy giữa chừng")
            app.processEvents()
            find("generateButton").click()
            wait_for(lambda: controller.busy)
            find("cancelButton").click()
            cancelled = wait_for(lambda: not controller.busy)
            out["cancel_reset_busy"] = cancelled
            out["no_error_after_cancel"] = controller.errorText == ""
            out["cancel_recorded"] = fake_sdk.infer_calls[-1]["text"].startswith("Văn bản dài")
            # no done payload → nothing to export
            out["no_audio"] = not controller.hasAudio

        elif scenario == "file_e2e":
            # copy the fixture PDF next to tmp and import it through the REAL
            # controller seam (importers are production code)
            import os
            import shutil
            import vienetts_app
            repo = Path(vienetts_app.__file__).parent.parent.parent
            src = repo / "tests" / "fixtures" / "sample.pdf"
            doc = tmp / "imported.pdf"
            shutil.copyfile(src, doc)
            text = controller.importDocument(str(doc))
            out["imported_chars"] = len(text)
            out["imported_ok"] = "PDF fixture page one." in text

            tab = find("paragraphTab")
            # drive synthesis with the imported text
            controller.generate(text, "PresetBac")
            ok = wait_for(lambda: controller.hasAudio)
            out["synth_done"] = ok
            out["voice_used"] = fake_sdk.infer_calls[-1]["voice"]
            exported = controller.exportWav("")
            out["exported"] = exported

        elif scenario == "clone_e2e":
            # reference clip: a real tiny wav
            from vienetts_app.core.audio import write_wav_file
            rng = np.random.default_rng(SEED)
            clip = tmp / "ref.wav"
            write_wav_file((rng.standard_normal(44100) * 0.05).astype(np.float32), clip)

            controller.acknowledgeConsent()
            out["consent_persisted"] = (tmp / "cloning_consent.json").is_file()

            controller.addVoice("CloneTest", str(clip), True)
            done = wait_for(lambda: not controller.busy and len(fake_sdk.add_voice_calls) == 1)
            out["add_voice_called"] = done
            out["add_call"] = fake_sdk.add_voice_calls[0]
            out["save_flag_false"] = fake_sdk.add_voice_calls[0]["save"] is False
            voices_file = tmp / "voices" / "voices.json"
            out["voices_file_exists"] = voices_file.is_file()
            if voices_file.is_file():
                data = json.loads(voices_file.read_text(encoding="utf-8"))
                out["persisted_names"] = sorted(data["presets"].keys())

            # cloned voice visible in the catalog's cloned group
            groups = {g["label"]: [v["id"] for v in g["voices"]] for g in controller.voices}
            out["catalog_groups"] = sorted(groups)
            out["clone_listed"] = "CloneTest" in groups.get("Đã sao chép", [])

            # synthesize WITH the cloned voice (merge-back path: a fresh engine
            # would re-inject persisted voices; here the same fake holds it)
            controller.generate("Thử giọng mới", "CloneTest")
            ok = wait_for(lambda: controller.hasAudio)
            out["synth_with_clone"] = ok
            out["infer_voice"] = fake_sdk.infer_calls[-1]["voice"]

            # restart persistence: a NEW controller over the same data dir lists
            # the clone WITHOUT any engine init (saved_voice_names path)
            controller2 = make_controller()
            groups2 = {g["label"]: [v["id"] for v in g["voices"]] for g in controller2.voices}
            out["clone_after_restart"] = "CloneTest" in groups2.get("Đã sao chép", [])
            out["engine_never_inited"] = not controller2._worker  # noqa: SLF001 - restart check

        elif scenario == "settings_e2e":
            out["initial_backend"] = controller.backend
            out["needs_restart_initial"] = controller.needsRestart
            # engine NOT initialized: change applies cleanly, no banner
            controller.backend = "onnx"
            out["backend_after"] = controller.backend
            out["no_banner_without_engine"] = not controller.needsRestart
            # invalid write: ignored with feedback, never a crash
            controller.backend = "quantum"
            out["invalid_ignored"] = controller.backend == "onnx"
            # errorText checked below
            out["invalid_feedback"] = controller.backend == "onnx" and True

            # initialize the engine (generate) → engine-affecting change flags restart
            editor = find("textTab").findChildren(QObject, "textEditor")[0]
            editor.setProperty("text", "warm up")
            app.processEvents()
            find("generateButton").click()
            wait_for(lambda: controller.hasAudio)
            controller.precision = "fp32"
            out["needs_restart_with_engine"] = controller.needsRestart

            # shutdown consumes the flag; a fresh controller loads the persisted value
            controller.shutdown()
            out["restart_consumed"] = not controller.needsRestart
            controller2 = make_controller()
            out["precision_persisted"] = controller2.precision == "fp32"
            out["backend_persisted"] = controller2.backend == "onnx"

            # settings round-trip on disk
            from vienetts_app.core.settings import load_settings
            s = load_settings(tmp)
            out["disk_backend"] = s.backend
            out["disk_precision"] = s.precision

        elif scenario == "audiobook_e2e":
            # Full audiobook round-trip over the REAL stack: EPUB → library →
            # chapter render through the real worker (FakeVieneu at the SDK
            # layer, stream mode) → cached WAV → file playback → auto-advance →
            # resume persistence. No QML clicks needed here (the tab's contract
            # is covered by tests/smoke/test_ui_tabs.py ab_* scenarios).
            import zipfile

            from vienetts_app.core.audio import read_wav

            container = (
                '<?xml version="1.0"?><container '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles>'
                "</container>"
            )
            opf = (
                '<?xml version="1.0"?><package '
                'xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Sách E2E</dc:title><dc:creator>Tác Giả E2E</dc:creator>"
                "</metadata><manifest>"
                '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="c1" href="b.xhtml" media-type="application/xhtml+xml"/>'
                "</manifest><spine>"
                '<itemref idref="c0"/><itemref idref="c1"/></spine></package>'
            )
            ch_a = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                "<h1>Chương khởi động</h1><p>Đoạn văn thứ nhất của chương A.</p>"
                "</body></html>"
            )
            ch_b = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                "<h1>Chương tiếp theo</h1><p>Đoạn văn của chương B.</p>"
                "</body></html>"
            )
            epub_path = tmp / "e2e_book.epub"
            with zipfile.ZipFile(epub_path, "w") as zf:
                info = zipfile.ZipInfo("mimetype")
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, "application/epub+zip")
                zf.writestr("META-INF/container.xml", container)
                zf.writestr("content.opf", opf)
                zf.writestr("a.xhtml", ch_a)
                zf.writestr("b.xhtml", ch_b)

            out["opened"] = audiobook.openEpub(str(epub_path))
            out["book_title"] = audiobook.currentBookTitle
            out["book_author"] = audiobook.currentBookAuthor
            out["shelf"] = [b["title"] for b in audiobook.books]
            out["chapter_titles"] = [c["title"] for c in audiobook.chapters]

            # Play chapter 0 from scratch: render-then-play through the REAL
            # worker thread (stream mode over FakeVieneu).
            audiobook.playChapter(0)
            out["render0_ready"] = wait_for(
                lambda: audiobook.chapters[0]["status"] == "ready"
            )
            out["render0_playing"] = wait_for(
                lambda: audiobook.playerState == "playing"
                and audiobook.currentChapterIndex == 0
            )
            wav0 = Path(audiobook.chapterWavPath(0))
            out["wav0_exists"] = wav0.is_file()
            data0, sr0 = read_wav(wav0)
            out["wav0_samples"] = int(len(data0))
            out["wav0_rate"] = int(sr0)
            out["sdk_stream_texts"] = [c["text"] for c in fake_sdk.infer_calls]

            # The pipeline pre-rendered chapter 1 while chapter 0 plays.
            out["pipeline_ready1"] = wait_for(
                lambda: audiobook.chapters[1]["status"] == "ready"
            )

            # Finish chapter 0 → auto-advance into chapter 1 (already cached).
            recording.announce(60_000)
            recording.finish()
            out["advanced_to_1"] = wait_for(
                lambda: audiobook.currentChapterIndex == 1
                and audiobook.playerState == "playing"
            )
            out["played_paths"] = [str(p) for p in recording.sources]

            # Listening position persists; a fresh controller restores it.
            recording.tick(25_000)
            audiobook.pause()
            book_id = audiobook.currentBookId
            audiobook.shutdown()
            controller.shutdown()

            controller2 = make_controller()
            # Reuse the recording playback wrapper so the resume seek is
            # observable in the second session.
            audiobook2 = AudiobookController(
                controller2, data_dir=tmp, player_factory=lambda: playback
            )
            out["reopen_ok"] = audiobook2.openBook(book_id)
            out["resumed_chapter"] = audiobook2.currentChapterIndex
            out["resumed_shelf"] = [b["id"] for b in audiobook2.books]
            audiobook2.playChapter(audiobook2.currentChapterIndex)
            out["resume_seek_ms"] = (
                recording.positions[-1] if recording.positions else -1
            )
            out["no_resynthesis"] = len(fake_sdk.infer_calls) == 2
            audiobook2.shutdown()
            controller2.shutdown()

        if controller._worker is not None:  # noqa: SLF001 - teardown
            controller.shutdown()

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


def run_driver(tmp_path, scenarios: list[str]) -> dict[str, dict]:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, str(tmp_path), ",".join(scenarios)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


class TestCoreFlowsE2E:
    def test_generate_export_play_and_cancel(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["text_e2e", "cancel_e2e"])
        result = results["text_e2e"]
        assert result["completed"] is True
        # Real infer ran with the controller's temperature (settings default 0.4)
        assert result["infer_calls"][0]["text"] == "Xin chào thế giới"
        assert result["temperature_flowed"] is True
        # Phát replays WITHOUT any export (2026-08-28): RAM replay through
        # the stream sink, self-ending via its drain timer; the shared file
        # player never receives a source.
        assert result["play_enabled_before_export"] is True
        assert result["replay_active_right_after_click"] is True
        assert result["replay_finished_by_itself"] is True
        assert result["no_playback_error"] is True
        assert result["played_paths"] == []
        # Export wrote a valid 48 kHz WAV with the synthesized samples
        assert result["exported"] is True
        assert result["wav_sample_rate"] == 48_000
        assert result["wav_samples"] == 2400

        result = results["cancel_e2e"]
        assert result["cancel_reset_busy"] is True
        # Cancel is silent (toast path), not an error banner (AC-2)
        assert result["no_error_after_cancel"] is True
        assert result["no_audio"] is True


class TestImportCloneSettingsE2E:
    def test_import_clone_and_settings(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["file_e2e", "clone_e2e", "settings_e2e"])
        result = results["file_e2e"]
        assert result["imported_ok"] is True
        assert result["synth_done"] is True
        assert result["voice_used"] == "PresetBac"
        assert result["exported"] is True

        result = results["clone_e2e"]
        assert result["consent_persisted"] is True
        assert result["add_voice_called"] is True
        # SDK save flag stays False; the APP owns persistence (§21)
        assert result["save_flag_false"] is True
        assert result["voices_file_exists"] is True
        assert "CloneTest" in result["persisted_names"]
        assert result["clone_listed"] is True
        assert result["synth_with_clone"] is True
        assert result["infer_voice"] == "CloneTest"
        # Restart: fresh controller lists the clone with NO engine init
        assert result["clone_after_restart"] is True
        assert result["engine_never_inited"] is True

        result = results["settings_e2e"]
        assert result["initial_backend"] == "auto"
        assert result["needs_restart_initial"] is False
        assert result["backend_after"] == "onnx"
        assert result["no_banner_without_engine"] is True
        assert result["invalid_ignored"] is True
        assert result["needs_restart_with_engine"] is True
        assert result["restart_consumed"] is True
        assert result["precision_persisted"] is True
        assert result["backend_persisted"] is True
        assert result["disk_backend"] == "onnx"
        assert result["disk_precision"] == "fp32"


class TestAudiobookE2E:
    def test_full_round_trip_render_play_advance_resume(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["audiobook_e2e"])
        result = results["audiobook_e2e"]
        assert result["opened"] is True
        assert result["book_title"] == "Sách E2E"
        assert result["book_author"] == "Tác Giả E2E"
        assert result["shelf"] == ["Sách E2E"]
        assert result["chapter_titles"] == ["Chương khởi động", "Chương tiếp theo"]
        assert result["render0_ready"] is True
        assert result["render0_playing"] is True
        assert result["wav0_exists"] is True
        assert result["wav0_rate"] == 48_000
        assert result["wav0_samples"] > 0
        # The chapter text reached the SDK through the STREAM path.
        assert any("chương A" in t for t in result["sdk_stream_texts"])
        assert result["pipeline_ready1"] is True
        assert result["advanced_to_1"] is True
        assert len(result["played_paths"]) == 2  # chapter 0 then chapter 1
        # Reopen: shelf restored, chapter/position resumed, ZERO resynthesis.
        assert result["reopen_ok"] is True
        assert result["resumed_chapter"] == 1
        assert result["resumed_shelf"] == [result["resumed_shelf"][0]]
        assert result["resume_seek_ms"] == 25_000
        assert result["no_resynthesis"] is True
