"""Measure process-to-QML startup milestones without loading a TTS model.

Parent mode (default): launch one fresh child process per iteration and own
the process-start timestamp. Child mode (``--child-output``): build the QML
shell once in this process and report QML milestones.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.benchmarks.corpus import get_corpus_entry
from scripts.benchmarks.schema import (
    BenchmarkRecord,
    BenchmarkScenario,
    environment_manifest,
    write_jsonl,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("fake", "real"), default="fake")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--hardware-class", default="unspecified")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=Path("benchmark-startup.jsonl"))
    parser.add_argument("--child-output", type=Path, default=None)
    return parser


def _run_child(output_path: Path, args: argparse.Namespace) -> int:
    """Child probe: build the QML shell once, report milestones as JSON."""

    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtQuick import QQuickWindow

    from vienetts_app.app import create_app
    from vienetts_app.ui.controller import AppController

    child_start_ns = time.perf_counter_ns()
    marks: dict[str, int] = {}
    frame_supported = False

    def mark(name: str) -> None:
        marks[name] = time.perf_counter_ns()

    with tempfile.TemporaryDirectory(prefix="vienetts-startup-") as data_dir:

        def controller_factory():
            return AppController(
                data_dir=Path(data_dir),
                engine_factory=lambda **_kwargs: None,
                worker_factory=lambda _engine: None,
                catalog=lambda: [],
                saved_names=lambda _voices_dir: [],
            )

        def observer(event: str) -> None:
            if event == "qml_loaded":
                mark("qml_loaded")

        try:
            app, engine = create_app(
                controller_factory=controller_factory,
                startup_observer=observer,
            )
            controller = engine._controller  # noqa: SLF001
            audiobook = engine._audiobook  # noqa: SLF001
            root = engine.rootObjects()[0]
            frame_received = False
            if isinstance(root, QQuickWindow):

                def on_exposed() -> None:
                    if "window_exposed" not in marks:
                        mark("window_exposed")

                def on_frame_swapped() -> None:
                    nonlocal frame_received, frame_supported
                    frame_received = True
                    frame_supported = True
                    if "first_frame_swapped" not in marks:
                        mark("first_frame_swapped")

                root.visibleChanged.connect(on_exposed)
                root.frameSwapped.connect(on_frame_swapped)
                if root.isVisible() or root.isExposed():
                    mark("window_exposed")
            if "qml_loaded" not in marks:
                mark("qml_loaded")
            deadline = time.monotonic() + float(args.timeout)
            while time.monotonic() < deadline and not frame_received:
                app.processEvents()
                QTimer.singleShot(0, lambda: None)
            with contextlib.suppress(Exception):
                audiobook.shutdown()
            with contextlib.suppress(Exception):
                controller.shutdown()
        except Exception:
            pass
    with contextlib.suppress(Exception):
        app_instance = QCoreApplication.instance()
        if app_instance is not None:
            app_instance.processEvents()
    payload = {
        "frame_signal_supported": frame_supported,
        "child_start_ns": child_start_ns,
        "events": [{"name": name, "ns": ns} for name, ns in sorted(marks.items())],
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    print(json.dumps(payload))
    return 0


def _events_to_map(events: object) -> dict[str, int]:
    mapping: dict[str, int] = {}
    if isinstance(events, dict):
        for key, value in events.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                mapping[str(key)] = int(value)
        return mapping
    if isinstance(events, list):
        for item in events:
            if isinstance(item, dict) and "name" in item and "ns" in item:
                try:
                    mapping[str(item["name"])] = int(item["ns"])  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
    return mapping


def _run_parent_iteration(args: argparse.Namespace) -> BenchmarkRecord:
    entry = get_corpus_entry("vi_20")
    parent_start_ns = time.perf_counter_ns()
    fd, tmp_name = tempfile.mkstemp(prefix="vienetts-startup-child-", suffix=".json")
    os.close(fd)
    tmp_path = Path(tmp_name)
    command = [
        sys.executable,
        "-m",
        "scripts.benchmarks.run_startup",
        "--child-output",
        str(tmp_path),
        "--engine",
        str(args.engine),
        "--hardware-class",
        str(args.hardware_class),
        "--timeout",
        str(args.timeout),
    ]
    try:
        # The child's own frame wait is bounded by --timeout; this outer bound
        # catches a child that deadlocks during Qt teardown on a headless
        # runner. A timed-out iteration degrades to the fallback milestone
        # below instead of hanging the parent (and the pytest layer) forever.
        subprocess.run(
            command,
            cwd=Path.cwd(),
            env={**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen")},
            capture_output=True,
            text=True,
            check=False,
            timeout=60.0,
        )
    except subprocess.TimeoutExpired:
        pass
    finally:
        parent_end_ns = time.perf_counter_ns()
    frame_supported = False
    event_ns: dict[str, int] = {}
    child_start_ns: int | None = None
    try:
        payload = json.loads(tmp_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            frame_supported = bool(payload.get("frame_signal_supported", False))
            raw_child_start = payload.get("child_start_ns")
            if isinstance(raw_child_start, (int, float)) and not isinstance(raw_child_start, bool):
                child_start_ns = int(raw_child_start)
            event_ns = _events_to_map(payload.get("events"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
    milestone_ns: int | None = None
    for candidate in ("first_frame_swapped", "window_exposed", "qml_loaded"):
        if candidate in event_ns:
            if candidate == "first_frame_swapped" and not frame_supported:
                continue
            milestone_ns = event_ns[candidate]
            break
    if milestone_ns is not None and milestone_ns >= parent_start_ns:
        process_cold_ns = milestone_ns - parent_start_ns
    else:
        process_cold_ns = parent_end_ns - parent_start_ns
    in_process_boot_ms: float | None = None
    if child_start_ns is not None and "qml_loaded" in event_ns:
        delta = event_ns["qml_loaded"] - child_start_ns
        if delta >= 0:
            in_process_boot_ms = delta / 1_000_000
    offsets = [{"name": "process_started", "offset_ms": 0.0}]
    for name in ("qml_loaded", "window_exposed", "first_frame_swapped"):
        if name in event_ns and event_ns[name] >= parent_start_ns:
            offsets.append(
                {"name": name, "offset_ms": (event_ns[name] - parent_start_ns) / 1_000_000}
            )
    trace: dict[str, object] = {
        "tags": {
            "run_kind": "startup",
            "mode": "infer",
            "streaming": False,
            "scenario_id": entry.scenario_id,
        },
        "events": offsets,
        "startup": {
            "process_start_parent_ns": parent_start_ns,
            "qml_loaded": "qml_loaded" in event_ns,
            "window_exposed": "window_exposed" in event_ns,
            "first_frame_swapped": "first_frame_swapped" in event_ns and frame_supported,
            "frame_signal_supported": frame_supported,
            "process_cold_startup_ms": process_cold_ns / 1_000_000,
            **(
                {"in_process_qml_boot_ms": in_process_boot_ms}
                if in_process_boot_ms is not None
                else {}
            ),
        },
    }
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend="startup",
        precision="none",
        mode="infer",
        sink_kind="none",
    )
    return BenchmarkRecord(
        environment=environment_manifest(hardware_class=args.hardware_class),
        scenario=scenario,
        trace=trace,
        resources={},
        elapsed_ns=int(process_cold_ns),
    )


def run(args: argparse.Namespace) -> int:
    if args.child_output is not None:
        return _run_child(Path(args.child_output), args)
    if args.iterations < 1:
        raise ValueError("iterations must be positive")
    records = [_run_parent_iteration(args) for _ in range(args.iterations)]
    write_jsonl(records, args.output)
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
