"""Offscreen end-to-end smoke suite (AC-1..AC-5, NFR-3.2/3.3, plan phase3 t1).

Fake-engine flows through the REAL QML shell and the REAL AppController:
cancel-mid-flight, generate → done → export → WAV valid, and file import →
synth → export — the three chains share ONE engine build; clone flow (fake
SDK add_voice with app-data persistence) shares its build with the settings
round-trip incl. apply-on-restart. Same subprocess+RESULT-JSON pattern as
the other GUI suites (one QGuiApplication per process).

The Qwen profiles (Phase 7 Task 7.2) run the same real stack against the
scripted fake HOST: a real ``QwenEngine`` spawns ``tests/unit/qwen_host_fake.py``
as a child process that speaks the framed protocol, with the model/runtime
managers answering "ready" from verified locations under the scenario's data
dir — so ready install, profile switch, CustomVoice synthesis, Base
enrollment/synthesis, replay/export, the Studio provenance guard, cancellation,
crash recovery and shutdown are all covered without a checkpoint, torch or a
download. Only the SDK and the host's model are faked; spawn, handshake,
streaming, cancel escalation and reaping are production code.

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
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

DRIVER = textwrap.dedent(
    """\
    import gc
    import json
    import os
    import sys
    import tempfile
    from pathlib import Path

    import numpy as np
    from PySide6.QtCore import QObject, QMetaObject, Q_ARG, QTimer, Signal, Slot
    from PySide6.QtQuick import QQuickItem

    import vienetts_app
    from vienetts_app.app import create_app
    from vienetts_app.core.detector import HardwareInfo
    from vienetts_app.core.engine import TTSEngine
    from vienetts_app.core.engine_profiles import QWEN_BASE, QWEN_CUSTOM
    from vienetts_app.core.qwen_engine import QwenEngine
    from vienetts_app.core.qwen_model_manager import QwenModelLocation, QwenModelStatus
    from vienetts_app.core.qwen_runtime import QwenRuntimeLocation, QwenRuntimeStatus
    from vienetts_app.core.voice_profiles import CloneStore
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.bg_ops import run_sync
    from vienetts_app.ui.controller import AppController
    from vienetts_app.workers.inference_worker import InferenceWorker

    # The scripted fake host (tests/unit/qwen_host_fake.py) speaks the real
    # framed protocol in a real child process, so the Qwen scenarios exercise
    # spawn/handshake/stream/cancel/reap with no torch and no checkpoint.
    _repo_root = Path(vienetts_app.__file__).resolve().parents[2]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from tests.unit import qwen_host_fake as host_fake

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

        # ── Qwen seams (inert unless a Qwen scenario arms them) ───────────
        # The isolated host runs the scripted fake — a real child process on the
        # real framed protocol — so every engine build spawns it with the mode
        # armed in ``qwen_mode`` and logs its frames to that build's own file.
        # The managers answer "ready" with verified locations under this data
        # dir: the ready-install posture ``_build_qwen_engine`` consumes, with
        # no Hub, no download and no checkpoint.
        qwen_mode = {"value": "ok"}
        qwen_hosts = []

        class FakeQwenModelManager:
            # Mirrors the real manager's root (<data>/qwen/models).
            def __init__(self, data_dir, profile_key):
                self.root = Path(data_dir) / "qwen" / "models"
                self.profile_key = profile_key

            def inspect(self):
                profile_dir = self.root / self.profile_key
                shared_dir = self.root / "shared"
                profile_dir.mkdir(parents=True, exist_ok=True)
                shared_dir.mkdir(parents=True, exist_ok=True)
                return QwenModelStatus(
                    state="ready",
                    profile_key=self.profile_key,
                    installed_bytes=1_234_567,
                    required_bytes=1_234_567,
                    location=QwenModelLocation(
                        root=self.root,
                        profile_dir=profile_dir,
                        shared_dir=shared_dir,
                        format_version="smoke",
                        profile_key=self.profile_key,
                        revision="smoke-rev",
                    ),
                )

        class FakeQwenRuntimeManager:
            # Mirrors the real manager's root (<data>/qwen/runtime).
            def __init__(self, data_dir):
                self.root = Path(data_dir) / "qwen" / "runtime"

            def inspect(self):
                site_packages = self.root / "site-packages"
                site_packages.mkdir(parents=True, exist_ok=True)
                return QwenRuntimeStatus(
                    state="ready",
                    platform_key="smoke",
                    installed_bytes=2_345_678,
                    required_bytes=2_345_678,
                    location=QwenRuntimeLocation(
                        root=self.root,
                        site_packages=site_packages,
                        format_version="smoke",
                        platform_key="smoke",
                        python_tag="smoke",
                    ),
                )

        qwen_model_managers = {}
        qwen_runtime_managers = {}

        def qwen_model_manager_factory(root, profile_key):
            key = (str(root), profile_key)
            if key not in qwen_model_managers:
                qwen_model_managers[key] = FakeQwenModelManager(root, profile_key)
            return qwen_model_managers[key]

        def qwen_runtime_manager_factory(root):
            key = str(root)
            if key not in qwen_runtime_managers:
                qwen_runtime_managers[key] = FakeQwenRuntimeManager(root)
            return qwen_runtime_managers[key]

        def qwen_engine_factory(**kwargs):
            mode = qwen_mode["value"]
            index = len(qwen_hosts) + 1
            entry = {
                "index": index,
                "mode": mode,
                "log": tmp / ("qwen_host_%d_%s.log" % (index, mode)),
                "model_dir": str(kwargs.get("model_dir", "")),
                "runtime_dir": str(kwargs.get("runtime_dir", "")),
                "device": str(kwargs.get("device", "")),
                "profile": str(kwargs.get("profile", "")),
            }
            entry["engine"] = QwenEngine(
                command=host_fake.fake_host(tmp, mode),
                environment={"FAKE_HOST_LOG": str(entry["log"])},
                handshake_timeout=5.0,
                load_timeout=5.0,
                frame_timeout=5.0,
                cancel_timeout=0.4,
                kill_timeout=1.0,
                shutdown_timeout=1.0,
                **kwargs,
            )
            qwen_hosts.append(entry)
            return entry["engine"]

        def host_frames(entry):
            path = Path(entry["log"])
            if not path.is_file():
                return []
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        def host_pid(entry):
            starts = [frame for frame in host_frames(entry) if frame.get("event") == "start"]
            return int(starts[-1]["pid"]) if starts else 0

        def host_alive(entry):
            pid = host_pid(entry)
            if not pid:
                return False
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            return True

        def host_frames_of(entry, frame_type):
            return [
                frame.get("fields", {})
                for frame in host_frames(entry)
                if frame.get("event") == "frame" and frame.get("type") == frame_type
            ]


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

            def worker_factory(eng, providers=None):
                # VieNeu keeps the single-engine default (no provider set);
                # a Qwen profile hands the worker its one-provider set.
                return InferenceWorker(eng, providers=providers)

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

            controller = AppController(
                bg_runner=run_sync,
                data_dir=tmp,
                engine_factory=engine_factory,
                worker_factory=worker_factory,
                stream_playback_factory=lambda: StreamPlaybackController(
                    sink_factory=lambda _fmt: fake_sink
                ),
                # Offscreen hosts expose zero output devices; these flows assert
                # playback UX, so assume a working device (FR-4.6a injectable probe).
                audio_probe=lambda: True,
                # A pinned CPU machine: the Qwen cards' device resolution must
                # not shell out (or import torch) from the GUI thread.
                hardware_probe=lambda: HardwareInfo(
                    kind="none", torch_installed=False, cuda_version=None
                ),
                qwen_model_manager_factory=qwen_model_manager_factory,
                qwen_runtime_manager_factory=qwen_runtime_manager_factory,
                qwen_engine_factory=qwen_engine_factory,
            )
            # Pin live mode: these scenarios assert live-session behavior and
            # predate the silent default.
            controller.livePreview = True
            return controller


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
                self.sources.append(url.toLocalFile())
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
        from vienetts_app.ui.chapter_persist import SyncPersistExecutor

        audiobook = AudiobookController(
            controller, data_dir=tmp, player_factory=lambda: playback,
            bg_runner=run_sync,
            persist_executor=SyncPersistExecutor(),
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
            # Cancel-mid-flight runs FIRST in this merged scenario: cancelling
            # needs a controller with NO committed artifact (no done payload
            # arrives, so hasAudio must stay False), which only holds before the
            # generate/import flows below. The slow SDK call is genuinely
            # in flight when cancel lands; the flows then continue on the same
            # engine build (one QML assembly for both claims).
            fake_sdk.infer_delay_ms = 900
            tab = find("textTab")
            editor = tab.findChildren(QObject, "textEditor")[0]
            editor.setProperty("text", "Văn bản dài để hủy giữa chừng")
            app.processEvents()
            find("generateButton").click()
            wait_for(lambda: controller.busy and len(fake_sdk.infer_calls) > 0)
            find("cancelButton").click()
            out["cancel_reset_busy"] = wait_for(lambda: not controller.busy)
            out["no_error_after_cancel"] = controller.errorText == ""
            out["cancel_recorded"] = bool(
                fake_sdk.infer_calls
                and fake_sdk.infer_calls[-1]["text"].startswith("Văn bản dài")
            )
            # no done payload → nothing to export
            out["no_audio"] = not controller.hasAudio
            fake_sdk.infer_delay_ms = 0  # the flows below assert fast jobs
            cancel_calls = len(fake_sdk.infer_calls)

            tab = find("textTab")
            editor = tab.findChildren(QObject, "textEditor")[0]
            editor.setProperty("text", "Xin chào thế giới")
            app.processEvents()
            # default voice preselected from settings (default "Adam" not in the
            # fake catalog → header-guard keeps controller default; assert via
            # generate call instead)
            find("generateButton").click()
            first_job_id = controller.foregroundJobId
            ok = wait_for(lambda: controller.hasAudio and not controller.busy)
            first_artifact_path = Path(controller.artifactPath)
            out["completed"] = ok
            # Windowed to the generate flow: index 0 is its first SDK call.
            out["infer_calls"] = fake_sdk.infer_calls[cancel_calls:]
            out["temperature_flowed"] = fake_sdk.infer_calls[cancel_calls]["temperature"] == 0.4
            out["first_job_id"] = first_job_id
            out["first_artifact_path"] = str(first_artifact_path)

            # Replay is artifact-backed through the shared file player.
            btn = tab.findChildren(QObject, "playButton")[0]
            out["play_enabled_before_export"] = btn.property("enabled")
            btn.click()
            app.processEvents()
            out["replay_active_right_after_click"] = controller.replayActive
            out["played_paths"] = list(recording.sources)
            recording.finish()
            out["replay_released_after_end_of_media"] = wait_for(
                lambda: not controller.replayActive
            )
            out["no_playback_error"] = controller.errorText == ""

            # A replacement synthesis retires the replayed artifact. Its
            # deletion proves PlaybackController's EndOfMedia callback
            # released the artifact-store protection, not merely replayActive.
            controller.generate("Bản tổng hợp thay thế", "PresetBac")
            second_job_id = controller.foregroundJobId
            second_artifact_path = (
                tmp / "artifacts" / "interactive" / f"{second_job_id}.wav"
            )
            out["replacement_completed"] = wait_for(
                lambda: (
                    not controller.busy
                    and controller.artifactPath == str(second_artifact_path)
                    and second_artifact_path.exists()
                )
            )
            out["first_artifact_deleted_after_replacement"] = wait_for(
                lambda: not first_artifact_path.exists()
            )

            # export to the default dir (settings output_dir = tmp)
            exported = controller.exportWav("")
            out["exported"] = exported
            out["last_export"] = controller.lastExportPath
            from vienetts_app.core.audio import read_wav
            import soundfile as sf
            data, sr = read_wav(controller.lastExportPath)
            out["wav_sample_rate"] = sr
            out["wav_samples"] = int(len(data))

            # Imported-document chain in the SAME process (one engine build):
            # copy the fixture PDF next to tmp and import it through the REAL
            # controller seam (importers are production code), then synthesize
            # the imported text and export it.
            import os
            import shutil
            import vienetts_app
            repo = Path(vienetts_app.__file__).parent.parent.parent
            src = repo / "tests" / "fixtures" / "sample.pdf"
            doc = tmp / "imported.pdf"
            shutil.copyfile(src, doc)
            got = {}
            controller.documentImported.connect(lambda p, t: got.update(text=t))
            assert controller.importDocument(str(doc)) is True
            imported = wait_for(lambda: "text" in got)
            text = got.get("text", "")
            out["file_imported_chars"] = len(text)
            out["file_imported_ok"] = imported and "PDF fixture page one." in text

            tab = find("paragraphTab")
            # drive synthesis with the imported text
            controller.generate(text, "PresetBac")
            ok = wait_for(lambda: controller.hasAudio)
            out["file_synth_done"] = ok
            out["file_voice_used"] = fake_sdk.infer_calls[-1]["voice"]
            exported = controller.exportWav("")
            out["file_exported"] = exported

        elif scenario == "clone_settings_e2e":
            # Settings seam FIRST (it needs the pre-engine posture): a backend
            # change without an engine applies with no restart banner, an
            # invalid write is ignored, and after the engine exists an
            # engine-affecting change flags a restart that shutdown consumes —
            # a fresh controller then reads the persisted backend/precision
            # back from disk. Same engine build as the clone flow below.
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
            settings_reload = make_controller()
            out["precision_persisted"] = settings_reload.precision == "fp32"
            out["backend_persisted"] = settings_reload.backend == "onnx"

            # settings round-trip on disk
            from vienetts_app.core.settings import load_settings
            s = load_settings(tmp)
            out["disk_backend"] = s.backend
            out["disk_precision"] = s.precision

            # Clone flow over the same data dir (engine re-inits lazily after
            # the shutdown above).
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
                controller2, data_dir=tmp, player_factory=lambda: playback,
                bg_runner=run_sync,
                persist_executor=SyncPersistExecutor(),
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

        elif scenario == "qwen_e2e":
            # One consolidated ready-install journey over the REAL stack: both
            # Qwen checkpoints report installed (pinned managers answer "ready"
            # with verified locations), the user switches profile, synthesizes
            # with a fixed speaker, replays and exports the artifact, enrolls a
            # Base clone and synthesizes with it, then meets the Studio
            # provenance guard — one engine build per profile, one real host
            # process per build (the scripted fake on the real protocol).
            controller.refreshProfileState()
            out["initial_profile"] = controller.engineProfile
            out["runtime_state"] = controller.qwenRuntimeState
            out["runtime_ready"] = controller.qwenRuntimeReady
            out["runtime_storage"] = controller.qwenRuntimeStoragePath
            out["model_storage"] = controller.qwenModelStoragePath
            out["models"] = {
                row["profile"]: (row["state"], row["ready"]) for row in controller.qwenModels
            }
            out["shared_bytes"] = int(controller.qwenSharedBytes)

            # ── profile switch: VieNeu → CustomVoice ─────────────────────
            bridge.setCurrentTab("settings")
            app.processEvents()
            settings_tab = find("settingsTab")
            combo = settings_tab.findChildren(QObject, "engineProfileCombo")[0]
            out["combo_index_before"] = int(combo.property("currentIndex"))
            out["combo_labels"] = [row["label"] for row in controller.engineProfiles]
            out["switch_custom"] = controller.switchEngineProfile(QWEN_CUSTOM)
            app.processEvents()
            out["profile"] = controller.engineProfile
            out["combo_index_after"] = int(combo.property("currentIndex"))
            out["readiness_text"] = settings_tab.findChildren(
                QObject, "engineProfileReadinessText"
            )[0].property("text")
            out["device_label"] = settings_tab.findChildren(
                QObject, "engineProfileDeviceLabel"
            )[0].property("text")
            out["profile_ready"] = controller.profileReady
            out["profile_model_state"] = controller.profileModelState
            out["profile_runtime_state"] = controller.profileRuntimeState
            out["device"] = controller.engineDevice
            out["voices"] = [voice["id"] for voice in controller.profileVoices]
            out["languages"] = [language["code"] for language in controller.profileLanguages]
            out["clones_custom"] = list(controller.profileClones)

            # ── CustomVoice synthesis through the QML shell ──────────────
            out["language_set"] = controller.setSynthesisLanguage("zh")
            out["language"] = controller.synthesisLanguage
            bridge.setCurrentTab("text")
            tab = find("textTab")
            picker = tab.findChildren(QObject, "voicePicker")[0]
            editor = tab.findChildren(QObject, "textEditor")[0]
            generate = tab.findChildren(QObject, "generateButton")[0]
            editor.setProperty("text", "你好，世界。")
            out["picker_voice"] = wait_for(lambda: picker.property("effectiveVoice") != "")
            out["picker_voice_id"] = picker.property("effectiveVoice")
            app.processEvents()
            out["generate_enabled"] = bool(generate.property("enabled"))
            generate.click()
            out["custom_job"] = controller.foregroundJobId
            out["custom_done"] = wait_for(lambda: controller.hasAudio and not controller.busy)
            custom_artifact = Path(controller.artifactPath)
            out["custom_artifact"] = str(custom_artifact)
            out["custom_artifact_exists"] = custom_artifact.is_file()
            from vienetts_app.core.audio import read_wav
            data, rate = read_wav(custom_artifact)
            out["custom_rate"] = int(rate)
            out["custom_samples"] = int(len(data))
            custom_host = qwen_hosts[-1]
            out["custom_host"] = {
                key: custom_host[key] for key in ("index", "mode", "profile", "device")
            }
            out["custom_model_dir"] = custom_host["model_dir"]
            out["custom_runtime_dir"] = custom_host["runtime_dir"]
            out["custom_load"] = host_frames_of(custom_host, "load")
            out["custom_synthesize"] = host_frames_of(custom_host, "synthesize")

            # ── artifact replay + export (engine-independent paths) ──────
            play = tab.findChildren(QObject, "playButton")[0]
            out["play_enabled"] = bool(play.property("enabled"))
            play.click()
            app.processEvents()
            out["replay_active"] = controller.replayActive
            out["played_paths"] = list(recording.sources)
            recording.finish()
            out["replay_released"] = wait_for(lambda: not controller.replayActive)
            out["exported"] = controller.exportWav("")
            export_path = Path(controller.lastExportPath)
            data, rate = read_wav(export_path)
            out["export_rate"] = int(rate)
            out["export_samples"] = int(len(data))
            out["no_error_after_export"] = controller.errorText == ""

            # A fixed-speaker profile refuses enrollment with the capability
            # table's reason — it never quietly enrolls into another engine.
            from vienetts_app.core.audio import write_wav_file
            rng = np.random.default_rng(SEED)
            clip = tmp / "qwen_ref.wav"
            write_wav_file((rng.standard_normal(48_000) * 0.05).astype(np.float32), clip, 24_000)
            controller.addVoice("FixedSpeakerClone", str(clip), True, "Xin chào")
            out["custom_clone_refused"] = wait_for(
                lambda: not controller.busy and controller.errorText != ""
            )
            out["custom_clone_error"] = controller.errorText
            out["custom_clone_store_empty"] = (
                CloneStore(tmp / "clones").list(profile=QWEN_CUSTOM) == ()
            )

            # ── Base: enrollment is required before synthesis ────────────
            out["switch_base"] = controller.switchEngineProfile(QWEN_BASE)
            out["base_ready"] = controller.profileReady
            out["base_voices"] = list(controller.profileVoices)
            out["base_clones_before"] = list(controller.profileClones)
            app.processEvents()
            out["base_generate_enabled"] = bool(generate.property("enabled"))
            out["base_generate_reason"] = generate.property("disabledReason")
            out["base_picker_reason"] = picker.property("unavailableReason")
            # The switch closed the previous engine's host: one model owner.
            out["custom_host_reaped"] = wait_for(
                lambda: not host_alive(custom_host), timeout_ms=4000
            )

            controller.acknowledgeConsent()
            out["consent"] = (tmp / "cloning_consent.json").is_file()
            controller.addVoice("QwenClone", str(clip), True, "Xin chào buổi sáng")
            out["enrolled"] = wait_for(
                lambda: not controller.busy and len(controller.profileClones) == 1
            )
            out["clone_rows"] = [
                (clone["id"], clone["label"], clone["transcript"])
                for clone in controller.profileClones
            ]
            out["clone_index_file"] = (tmp / "clones" / "clones.json").is_file()
            out["clone_references"] = sorted(
                path.name for path in (tmp / "clones" / "references").iterdir()
            )
            store = CloneStore(tmp / "clones")
            out["clone_profiles"] = sorted({clone.profile for clone in store.list()})
            out["picker_voice_base"] = wait_for(
                lambda: picker.property("effectiveVoice") != ""
            ) and picker.property("effectiveVoice")
            out["picker_rows"] = [dict(row)["label"] for row in picker.property("model")]

            # ── Base synthesis with the enrolled clone ───────────────────
            generate.click()
            out["base_done"] = wait_for(lambda: controller.hasAudio and not controller.busy)
            base_artifact = Path(controller.artifactPath)
            data, rate = read_wav(base_artifact)
            out["base_rate"] = int(rate)
            out["base_samples"] = int(len(data))
            base_host = qwen_hosts[-1]
            out["base_model_dir"] = base_host["model_dir"]
            out["base_synthesize"] = host_frames_of(base_host, "synthesize")

            # ── Studio: provenance guard + engine-independent edit/export ─
            bridge.setCurrentTab("studio")
            out["studio_open"] = controller.openInStudio("interactive", "你好，世界。")
            app.processEvents()
            clip_row = controller.studioClips[0]
            out["studio_clip"] = {
                key: clip_row[key] for key in ("id", "profile", "profileLabel", "language")
            }
            # Repeater delegates live in the ITEM tree (see test_ui_tabs.py).
            out["studio_clip_profile_text"] = ifind("studioClipProfile")[0].property("text")
            out["studio_clip_language_text"] = ifind("studioClipLanguage")[0].property("text")

            # Back to VieNeu: the clip's audio is Base's, so re-synthesis is
            # refused with the profile it needs, not silently re-rendered.
            out["switch_vieneu"] = controller.switchEngineProfile("vieneu")
            out["regen_refused"] = controller.studioRegenClip(clip_row["id"], "PresetBac")
            out["regen_armed"] = controller.studioRegenProfile
            out["regen_armed_label"] = controller.studioRegenProfileLabel
            out["regen_error"] = controller.errorText
            app.processEvents()
            out["banner_visible"] = bool(
                ifind("studioRegenProfileBanner")[0].property("visible")
            )
            out["banner_text"] = ifind("studioRegenProfileLabel")[0].property("text")
            out["banner_button_text"] = ifind("studioSwitchToRegenProfileButton")[0].property(
                "text"
            )
            ifind("studioSwitchToRegenProfileButton")[0].click()
            app.processEvents()
            out["banner_switched"] = controller.engineProfile == QWEN_BASE
            out["banner_consumed"] = controller.studioRegenProfile == ""

            out["regen_accepted"] = controller.studioRegenClip(
                clip_row["id"], "QwenClone", "Câu mới trong Studio"
            )
            out["regen_done"] = wait_for(
                lambda: not controller.busy
                and controller.studioClips[0]["text"] == "Câu mới trong Studio"
            )
            out["regen_clip"] = {
                key: controller.studioClips[0][key]
                for key in ("profile", "profileLabel", "language")
            }
            # Editing and export never consult provenance.
            out["studio_gain"] = controller.studioPushGain(-3.0)
            out["studio_preview"] = controller.studioPreview()
            studio_export = tmp / "studio_export.wav"
            out["studio_export"] = controller.studioExport(str(studio_export))
            out["studio_export_done"] = wait_for(lambda: studio_export.is_file())
            data, rate = read_wav(studio_export)
            out["studio_export_rate"] = int(rate)
            out["studio_export_samples"] = int(len(data))

            # ── shutdown + restart: the clone survives, no host lingers ──
            controller.shutdown()
            out["base_host_reaped"] = wait_for(lambda: not host_alive(base_host), timeout_ms=4000)
            out["hosts_started"] = len(qwen_hosts)
            out["hosts_reaped"] = [not host_alive(entry) for entry in qwen_hosts]
            # Every frame each host RECEIVED, in order: the protocol trail.
            out["host_frames"] = {
                str(entry["index"]): [
                    frame["type"] for frame in host_frames(entry) if frame.get("event") == "frame"
                ]
                for entry in qwen_hosts
            }

            controller2 = make_controller()
            out["restart_profile"] = controller2.engineProfile
            out["restart_clones"] = [
                (clone["label"], clone["transcript"]) for clone in controller2.profileClones
            ]
            out["restart_no_engine"] = controller2._worker is None  # noqa: SLF001 - restart check
            out["restart_no_host"] = len(qwen_hosts) == out["hosts_started"]
            from vienetts_app.core.settings import load_settings
            out["disk_profile"] = load_settings(tmp).engine_profile
            controller2.shutdown()

        elif scenario == "qwen_recovery_e2e":
            # Failure/recovery journey: a user cancel keeps the host alive, a
            # profile switch is refused while the job runs, a host that dies
            # mid-job fails the job with the crash's own diagnostics, and the
            # app stays usable — the next submission gets a fresh host — before
            # shutdown reaps everything.
            controller.refreshProfileState()
            out["switch_custom"] = controller.switchEngineProfile(QWEN_CUSTOM)
            bridge.setCurrentTab("text")
            tab = find("textTab")
            editor = tab.findChildren(QObject, "textEditor")[0]
            cancel = tab.findChildren(QObject, "cancelButton")[0]

            # ── cancellation: one cancelled terminal, host stays up ──────
            qwen_mode["value"] = "graceful_cancel"
            editor.setProperty("text", "你好，世界。")
            app.processEvents()
            find("generateButton").click()
            out["busy_started"] = wait_for(lambda: controller.busy)
            cancel_host = qwen_hosts[-1]
            out["host_saw_synthesize"] = wait_for(
                lambda: bool(host_frames_of(cancel_host, "synthesize"))
            )
            # One model owner: the switch is refused while the job runs.
            out["switch_refused_while_busy"] = (
                controller.switchEngineProfile(QWEN_BASE) is False
            )
            out["profile_unchanged"] = controller.engineProfile == QWEN_CUSTOM
            out["switch_refusal_error"] = controller.errorText
            cancel.click()
            out["cancel_reset_busy"] = wait_for(lambda: not controller.busy)
            out["cancel_state"] = controller.foregroundJobState
            out["cancel_no_artifact"] = not controller.hasAudio
            out["cancel_host_alive"] = host_alive(cancel_host)
            out["cancel_artifact_absent"] = not any(
                path.name.endswith(".part") for path in (tmp / "artifacts").rglob("*")
            )

            # ── crash recovery: the host dies mid-job ────────────────────
            qwen_mode["value"] = "crash_after_pcm"
            controller.shutdown()
            out["cancel_host_reaped"] = wait_for(
                lambda: not host_alive(cancel_host), timeout_ms=4000
            )
            editor.setProperty("text", "崩溃测试")
            app.processEvents()
            find("generateButton").click()
            out["crash_failed"] = wait_for(
                lambda: not controller.busy and controller.errorText != ""
            )
            out["crash_state"] = controller.foregroundJobState
            out["crash_error"] = controller.errorText
            out["crash_no_artifact"] = not controller.hasAudio
            crash_host = qwen_hosts[-1]
            out["crash_host"] = crash_host["mode"]
            out["crash_starts"] = len(
                [frame for frame in host_frames(crash_host) if frame.get("event") == "start"]
            )

            # ── usable again: a fresh submission gets a fresh host ───────
            qwen_mode["value"] = "ok"
            controller.shutdown()
            # Closing the crashed engine reaps the dead child (no zombie).
            out["crash_host_reaped"] = wait_for(
                lambda: not host_alive(crash_host), timeout_ms=4000
            )
            editor.setProperty("text", "恢复测试")
            app.processEvents()
            find("generateButton").click()
            out["recovered"] = wait_for(lambda: controller.hasAudio and not controller.busy)
            out["recovered_error"] = controller.errorText
            recovered_host = qwen_hosts[-1]
            out["recovered_mode"] = recovered_host["mode"]
            out["recovered_host_new"] = recovered_host["index"] != crash_host["index"]
            out["hosts_started"] = len(qwen_hosts)

            # ── shutdown: every host is reaped, nothing lingers ─────────
            controller.shutdown()
            out["all_hosts_reaped"] = wait_for(
                lambda: not any(host_alive(entry) for entry in qwen_hosts), timeout_ms=4000
            )
            out["hosts_reaped"] = [not host_alive(entry) for entry in qwen_hosts]
            # Every frame each host RECEIVED, in order: the protocol trail.
            out["host_frames"] = {
                str(entry["index"]): [
                    frame["type"] for frame in host_frames(entry) if frame.get("event") == "frame"
                ]
                for entry in qwen_hosts
            }

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
    # The driver is ~50 KB of Python — Windows CreateProcess caps the whole
    # command line at ~32 KB (WinError 206), so it must run from a file, not
    # `python -c`. Script mode drops cwd from sys.path, hence PYTHONPATH.
    driver_path = tmp_path / "_driver.py"
    driver_path.write_text(DRIVER, encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        # Root for repo-level imports, src so a SHARED venv (editable install
        # pinned to another checkout/worktree) still resolves THIS tree's
        # package first — a no-op when the venv's editable target is this repo.
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


class TestCoreFlowsE2E:
    def test_generate_export_play_and_cancel(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["text_e2e"])
        result = results["text_e2e"]
        # Cancel-mid-flight runs first, before any artifact exists.
        assert result["cancel_reset_busy"] is True
        # Cancel is silent (toast path), not an error banner (AC-2)
        assert result["no_error_after_cancel"] is True
        assert result["cancel_recorded"] is True
        assert result["no_audio"] is True

        assert result["completed"] is True
        # Real infer ran with the controller's temperature (settings default 0.4)
        assert result["infer_calls"][0]["text"] == "Xin chào thế giới"
        assert result["temperature_flowed"] is True
        # Replay hands the managed WAV artifact to the shared file player and
        # EndOfMedia releases the replay state.
        assert result["play_enabled_before_export"] is True
        assert result["replay_active_right_after_click"] is True
        assert result["replay_released_after_end_of_media"] is True
        assert result["no_playback_error"] is True
        assert len(result["played_paths"]) == 1
        replay_path = Path(result["played_paths"][0])
        assert replay_path == (
            tmp_path / "text_e2e" / "artifacts" / "interactive" / f"{result['first_job_id']}.wav"
        )
        assert result["first_artifact_path"] == str(replay_path)
        assert result["replacement_completed"] is True
        assert result["first_artifact_deleted_after_replacement"] is True
        # Export wrote a valid 48 kHz WAV with the synthesized samples
        assert result["exported"] is True
        assert result["wav_sample_rate"] == 48_000
        assert result["wav_samples"] == 2400
        # Imported-document chain (fixture PDF → REAL importDocument →
        # generate → exportWav) runs in the SAME scenario/engine build.
        assert result["file_imported_ok"] is True
        assert result["file_synth_done"] is True
        assert result["file_voice_used"] == "PresetBac"
        assert result["file_exported"] is True


class TestImportCloneSettingsE2E:
    def test_import_clone_and_settings(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["clone_settings_e2e"])
        result = results["clone_settings_e2e"]
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

        # Settings seam, folded into the same scenario/engine build.
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


class TestQwenProfilesE2E:
    def test_ready_install_profiles_clone_synthesis_and_studio_guard(self, tmp_path) -> None:
        """Task 7.2: ready install → profile switch → CustomVoice synthesis →
        replay/export → Base enrollment/synthesis → Studio guard → shutdown,
        over the real controller/worker and a real (scripted) host process."""
        results = run_driver(tmp_path, ["qwen_e2e"])
        result = results["qwen_e2e"]
        data_dir = tmp_path / "qwen_e2e"

        # ── ready install: both checkpoints report installed ─────────────
        assert result["initial_profile"] == "vieneu"  # AC-1: VieNeu is the default
        assert result["runtime_state"] == "ready"
        assert result["runtime_ready"] is True
        assert result["models"] == {
            "qwen_custom_0_6b": ["ready", True],
            "qwen_base_0_6b": ["ready", True],
        }
        assert result["shared_bytes"] > 0  # the tokenizer download both reuse
        assert result["runtime_storage"] == str(data_dir / "qwen" / "runtime")
        assert result["model_storage"] == str(data_dir / "qwen" / "models")

        # ── profile switch through the shared control ────────────────────
        assert result["combo_labels"] == [
            "VieNeu-TTS v3 Turbo",
            "Qwen3-TTS CustomVoice 0.6B",
            "Qwen3-TTS Base 0.6B",
        ]
        assert result["combo_index_before"] == 0
        assert result["switch_custom"] is True
        assert result["profile"] == "qwen_custom_0_6b"
        assert result["combo_index_after"] == 1
        assert result["readiness_text"] == "Sẵn sàng"
        assert result["device_label"] == "Thiết bị: CPU"  # pinned CPU probe
        assert result["profile_ready"] is True
        assert result["profile_model_state"] == "ready"
        assert result["profile_runtime_state"] == "ready"
        assert result["device"] == "cpu"
        assert result["voices"] == [
            "Vivian",
            "Serena",
            "Uncle_Fu",
            "Dylan",
            "Eric",
            "Ryan",
            "Aiden",
            "Ono_Anna",
            "Sohee",
        ]
        assert result["languages"] == [
            "auto",
            "zh",
            "en",
            "ja",
            "ko",
            "de",
            "fr",
            "ru",
            "pt",
            "es",
            "it",
        ]
        assert result["clones_custom"] == []  # fixed speakers, no clones

        # ── CustomVoice synthesis: QML click → real host → artifact ──────
        assert result["language_set"] is True
        assert result["language"] == "zh"
        assert result["picker_voice"] is True
        assert result["picker_voice_id"] == "Vivian"  # first pinned speaker
        assert result["generate_enabled"] is True
        assert result["custom_done"] is True
        assert result["custom_artifact_exists"] is True
        assert result["custom_artifact"] == str(
            data_dir / "artifacts" / "interactive" / f"{result['custom_job']}.wav"
        )
        assert result["custom_rate"] == 48_000
        assert result["custom_samples"] == 24_000  # two scripted 12k chunks
        # The host was handed the VERIFIED installs, not a guess.
        assert result["custom_host"] == {
            "index": 1,
            "mode": "ok",
            "profile": "qwen_custom_0_6b",
            "device": "cpu",
        }
        assert result["custom_model_dir"] == str(data_dir / "qwen" / "models" / "customvoice")
        assert result["custom_runtime_dir"] == str(data_dir / "qwen" / "runtime" / "site-packages")
        assert result["custom_load"][0]["profile"] == "customvoice"
        assert result["custom_load"][0]["modelDir"] == result["custom_model_dir"]
        assert result["custom_load"][0]["device"] == "cpu"
        # The job crossed IPC with the context's language + selected speaker.
        assert result["custom_synthesize"] == [
            {"text": "你好，世界。", "language": "Chinese", "speaker": "Vivian"}
        ]
        # The protocol trail: the host was loaded, synthesized, and shut down.
        assert result["host_frames"]["1"] == ["load", "synthesize", "shutdown"]

        # ── artifact replay + export stay engine-independent ─────────────
        assert result["play_enabled"] is True
        assert result["replay_active"] is True
        assert result["played_paths"] == [result["custom_artifact"]]
        assert result["replay_released"] is True
        assert result["exported"] is True
        assert result["export_rate"] == 48_000
        assert result["export_samples"] == 24_000
        assert result["no_error_after_export"] is True

        # A fixed-speaker profile refuses enrollment with the reason (AC-4).
        assert result["custom_clone_refused"] is True
        assert "uses fixed speakers" in result["custom_clone_error"]
        assert result["custom_clone_store_empty"] is True

        # ── Base: nothing to synthesize with until a clone is enrolled ───
        assert result["switch_base"] is True
        assert result["base_ready"] is True
        assert result["base_voices"] == []
        assert result["base_clones_before"] == []
        assert result["base_generate_enabled"] is False
        assert "hãy tạo một giọng" in result["base_generate_reason"]
        assert "hãy tạo một giọng" in result["base_picker_reason"]
        # The switch closed the previous engine's host: one model owner.
        assert result["custom_host_reaped"] is True

        assert result["consent"] is True
        assert result["enrolled"] is True
        assert result["clone_rows"] == [
            [result["clone_rows"][0][0], "QwenClone", "Xin chào buổi sáng"]
        ]
        assert result["clone_rows"][0][0]  # a real clone id
        assert result["clone_index_file"] is True
        assert len(result["clone_references"]) == 1
        assert result["clone_references"][0].endswith(".wav")
        assert result["clone_profiles"] == ["qwen_base_0_6b"]  # catalogs stay separate
        assert result["picker_voice_base"] == result["clone_rows"][0][0]
        assert result["picker_rows"] == ["▸ Giọng đã sao chép", "— QwenClone"]

        # ── Base synthesis renders the enrolled clone through the host ───
        assert result["base_done"] is True
        assert result["base_rate"] == 48_000
        assert result["base_samples"] == 24_000
        assert result["base_model_dir"] == str(data_dir / "qwen" / "models" / "base")
        (base_call,) = result["base_synthesize"]
        assert base_call["language"] == "Chinese"
        assert "speaker" not in base_call  # a clone is a prompt, not a speaker
        assert base_call["refText"] == "Xin chào buổi sáng"
        assert Path(base_call["voicePrompt"]).is_file()
        assert Path(base_call["voicePrompt"]).parent == data_dir / "clones" / "references"
        assert result["host_frames"]["2"] == ["load", "synthesize", "shutdown"]

        # ── Studio: truthful provenance + the mismatch guard ────────────
        assert result["studio_open"] is True
        assert result["studio_clip"]["profile"] == "qwen_base_0_6b"
        assert result["studio_clip"]["profileLabel"] == "Qwen3-TTS Base 0.6B"
        assert result["studio_clip"]["language"] == "zh"
        assert result["studio_clip_profile_text"] == "Hồ sơ: Qwen3-TTS Base 0.6B"
        assert result["studio_clip_language_text"] == "Ngôn ngữ: zh"

        assert result["switch_vieneu"] is True
        assert result["regen_refused"] is False  # never silently re-rendered
        assert result["regen_armed"] == "qwen_base_0_6b"
        assert result["regen_armed_label"] == "Qwen3-TTS Base 0.6B"
        assert "Qwen3-TTS Base 0.6B" in result["regen_error"]
        assert result["banner_visible"] is True
        assert "Qwen3-TTS Base 0.6B" in result["banner_text"]
        assert result["banner_button_text"] == "Chuyển sang Qwen3-TTS Base 0.6B"
        # The banner's own action performs the switch and consumes the offer.
        assert result["banner_switched"] is True
        assert result["banner_consumed"] is True

        assert result["regen_accepted"] is True
        assert result["regen_done"] is True
        # The clip stays Base's. Its language is Base's default again: the
        # stored "zh" was dropped when VieNeu (which cannot serve it) became
        # active, and the switch back re-applies the profile default.
        assert result["regen_clip"] == {
            "profile": "qwen_base_0_6b",
            "profileLabel": "Qwen3-TTS Base 0.6B",
            "language": "auto",
        }
        # Editing and export never consult provenance.
        assert result["studio_gain"] is True
        assert result["studio_preview"] is True
        assert result["studio_export"] is True
        assert result["studio_export_done"] is True
        assert result["studio_export_rate"] == 48_000
        assert result["studio_export_samples"] > 0

        # ── shutdown + restart: no host lingers, the clone survives ─────
        assert result["base_host_reaped"] is True
        assert result["hosts_started"] == 3  # CustomVoice, Base, Base regen
        assert result["hosts_reaped"] == [True, True, True]
        # Each host got exactly the load it needed and a clean shutdown.
        assert result["host_frames"] == {
            "1": ["load", "synthesize", "shutdown"],
            "2": ["load", "synthesize", "shutdown"],
            "3": ["load", "synthesize", "shutdown"],
        }
        assert result["restart_profile"] == "qwen_base_0_6b"
        assert result["restart_clones"] == [["QwenClone", "Xin chào buổi sáng"]]
        assert result["restart_no_engine"] is True  # startup stays model-free
        assert result["restart_no_host"] is True
        assert result["disk_profile"] == "qwen_base_0_6b"


class TestQwenRecoveryE2E:
    def test_cancel_switch_guard_crash_recovery_and_shutdown(self, tmp_path) -> None:
        """Task 7.2: a user cancel settles one cancelled terminal and keeps the
        host alive, a switch is refused mid-job, a host crash fails the job with
        its own diagnostics, and the app recovers with a fresh host (AC-7/AC-8)."""
        results = run_driver(tmp_path, ["qwen_recovery_e2e"])
        result = results["qwen_recovery_e2e"]

        # ── cancellation: cancel request → cancelled terminal ────────────
        assert result["switch_custom"] is True
        assert result["busy_started"] is True
        assert result["host_saw_synthesize"] is True  # genuinely in flight
        assert result["switch_refused_while_busy"] is True  # AC-7: one owner
        assert result["profile_unchanged"] is True
        assert "Không thể đổi engine" in result["switch_refusal_error"]
        assert result["cancel_reset_busy"] is True
        assert result["cancel_state"] == "cancelled"
        assert result["cancel_no_artifact"] is True
        assert result["host_frames"]["1"] == ["load", "synthesize", "cancel", "shutdown"]
        assert result["cancel_host_alive"] is True  # a user cancel spares the host
        assert result["cancel_artifact_absent"] is True  # no partial file left

        # ── crash recovery: the host dies mid-job ────────────────────────
        assert result["cancel_host_reaped"] is True  # shutdown reaps it
        assert result["crash_failed"] is True
        assert result["crash_state"] == "failed"
        assert "closed its output stream" in result["crash_error"]
        assert "boom: the host died mid-job" in result["crash_error"]  # stderr tail
        assert result["crash_no_artifact"] is True
        assert result["crash_host"] == "crash_after_pcm"
        assert result["crash_host_reaped"] is True  # the dead child is reaped
        assert result["crash_starts"] == 1  # no silent respawn of a dead host
        # The crashed host never saw a shutdown: it died mid-job.
        assert result["host_frames"]["2"] == ["load", "synthesize"]

        # ── usable again: a fresh submission gets a fresh host ───────────
        assert result["recovered"] is True
        assert result["recovered_error"] == ""  # the next job cleared the banner
        assert result["recovered_mode"] == "ok"
        assert result["recovered_host_new"] is True
        assert result["host_frames"]["3"] == ["load", "synthesize", "shutdown"]
        assert result["hosts_started"] == 3  # cancel, crash, recovered

        # ── shutdown: nothing lingers ───────────────────────────────────
        assert result["all_hosts_reaped"] is True
        assert result["hosts_reaped"] == [True, True, True]


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
