"""Packaging contract for the isolated **GGUF** Qwen host (Task 6.1).

The frozen app re-dispatches ITSELF as the qwentts.cpp host
(``<exe> --qwen-gguf-host``) — there is no ``python -m`` to hand a module
name to inside a bundle, and the managed runtime pack is native-only (it
carries no Python at all), so the host half must ship inside the app.
These tests pin:

* the flag dispatch happens before GUI/stdio setup, in source AND frozen
  command construction;
* the spec bundles the host + ABI modules and the manifest data, while
  keeping the runtime-only stack excluded;
* the Windows spawn stays windowless and the child cwd is the verified
  pack directory (ggml discovers backends there, never via cwd/PATH);
* a missing/ABI-broken library surfaces as a recoverable error frame, not
  a crashed host.
"""

from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

from vienetts_app.__main__ import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "packaging" / "vienetts-app.spec"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
GGUF_HOST_MODULE_PATH = PROJECT_ROOT / "src" / "vienetts_app" / "workers" / "qwen_gguf_host.py"


class TestFrozenGgufHostCommand:
    def test_a_source_checkout_runs_the_host_module(self) -> None:
        from vienetts_app.core.qwen_gguf_engine import GGUF_HOST_MODULE, gguf_host_command

        assert gguf_host_command() == [sys.executable, "-m", GGUF_HOST_MODULE]

    def test_a_frozen_build_redispatches_the_packaged_executable(self, monkeypatch) -> None:
        from vienetts_app.core.qwen_engine import is_frozen
        from vienetts_app.core.qwen_gguf_engine import GGUF_HOST_FLAG, gguf_host_command

        assert is_frozen() is False
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        # The pack carries no Python, so the app itself becomes the host.
        assert gguf_host_command() == [sys.executable, GGUF_HOST_FLAG]

    def test_the_cli_routes_the_gguf_flag_without_reaching_the_gui(self, monkeypatch) -> None:
        from vienetts_app.core.qwen_gguf_engine import GGUF_HOST_FLAG
        from vienetts_app.workers import qwen_gguf_host

        calls: list[str] = []
        monkeypatch.setattr(qwen_gguf_host, "main", lambda *a, **kw: calls.append("host") or 7)
        assert main([GGUF_HOST_FLAG]) == 7
        assert calls == ["host"]

    def test_the_gguf_flag_is_routed_before_the_gui_stdio_setup(self, monkeypatch) -> None:
        # stdout is the frame channel — the windowed-exe stdio safety net
        # must never touch it (the native lib writes to fd 1 pre-isolation).
        import vienetts_app
        from vienetts_app.core.qwen_gguf_engine import GGUF_HOST_FLAG
        from vienetts_app.workers import qwen_gguf_host

        touched: list[str] = []
        monkeypatch.setattr(vienetts_app, "ensure_windowed_stdio", lambda: touched.append("stdio"))
        monkeypatch.setattr(qwen_gguf_host, "main", lambda *a, **kw: 0)
        assert main([GGUF_HOST_FLAG]) == 0
        assert touched == []

    def test_the_gguf_flag_does_not_swallow_the_gui_path(self) -> None:
        assert main([], gui_runner=lambda: 3) == 3


class TestGgufHostSpawn:
    def test_the_windows_spawn_is_windowless_and_cwd_is_the_pack(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # console=False app + a spawn without CREATE_NO_WINDOW = a console
        # flash per synthesis. The cwd must be the verified pack dir — ggml
        # discovers its backend modules there, never via PATH.
        from vienetts_app.core import qwen_engine as qwen_engine_module
        from vienetts_app.core.qwen_engine import QwenEngineError
        from vienetts_app.core.qwen_gguf_engine import (
            GGUF_HOST_MODULE,
            QwenGgufEngine,
        )

        recorded: dict[str, object] = {}

        def recording_popen(command, **kwargs):  # noqa: ANN001, ANN003 — Popen signature
            recorded["command"] = command
            recorded.update(kwargs)
            raise OSError("stop before a real spawn")

        monkeypatch.setattr(qwen_engine_module.subprocess, "Popen", recording_popen)
        monkeypatch.setattr(
            qwen_engine_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
        )
        monkeypatch.setattr(qwen_engine_module, "IS_WINDOWS", True)
        runtime_dir = tmp_path / "qwentts" / "linux-x64-cpu"
        runtime_dir.mkdir(parents=True)
        engine = QwenGgufEngine(
            profile="qwen_custom_0_6b",
            runtime_dir=runtime_dir,
            talker_path=tmp_path / "talker.gguf",
            codec_path=tmp_path / "codec.gguf",
            quantization="Q8_0",
            device="cpu",
        )
        with pytest.raises(QwenEngineError):
            engine.initialize()
        assert recorded["creationflags"] == 0x08000000
        assert recorded["shell"] is False
        assert recorded["cwd"] == str(runtime_dir)
        assert recorded["command"] == [
            sys.executable,
            "-m",
            GGUF_HOST_MODULE,
        ]
        # The pack is native-only: no PYTHONPATH/runtime env leaks into it.
        env = engine._host_environment()  # noqa: SLF001
        assert "PYTHONPATH" not in env or str(runtime_dir) not in env["PYTHONPATH"]


class TestGgufHostModuleContract:
    """The host's import surface decides what the bundle must ship/exclude."""

    def test_the_module_imports_without_the_native_library(self) -> None:
        # CDLL runs at `load` time only — a frozen bundle with no pack must
        # still import the host module (it is the hello frame's source).
        import vienetts_app.workers.qwen_gguf_host as host_module

        assert host_module.GGUF_HOST_NAME

    def test_module_level_third_party_imports_stay_bundled(self) -> None:
        import ast

        from tests.unit.test_package import _spec_call, _spec_keyword  # noqa: PLC0415

        tree = ast.parse(GGUF_HOST_MODULE_PATH.read_text(encoding="utf-8"))
        module_level: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_level |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                module_level.add(node.module.split(".")[0])
        third_party = {
            name
            for name in module_level
            if name not in sys.stdlib_module_names and name != "vienetts_app"
        }
        # numpy is the host's only third-party module-level need; it is an
        # app dependency and stays bundled. A NEW heavy import here means a
        # frozen host that dies at import — this test is the tripwire.
        assert third_party == {"numpy"}
        excluded = {
            constant.value
            for constant in _spec_keyword(_spec_call("Analysis"), "excludes").elts
            if isinstance(constant, ast.Constant)
        }
        assert not (third_party & excluded)


class TestGgufSpecContract:
    def test_the_gguf_host_modules_ship_explicitly(self) -> None:
        # collect_submodules already reaches them; the explicit list is the
        # contract that a spec refactor can never silently drop the host half.
        text = SPEC_PATH.read_text(encoding="utf-8")
        assert '"vienetts_app.workers.qwen_gguf_host"' in text
        assert '"vienetts_app.workers.qwen_gguf_abi"' in text

    def test_no_native_pack_or_model_tree_is_bundled(self) -> None:
        # GGUF runtime packs and .gguf weights install into the app data dir
        # at run time — shipping them would double the bundle and ship
        # unverified binaries. The literal datas block must stay app-only.
        from tests.unit.test_package import _spec_datas_literal  # noqa: PLC0415

        entries = _spec_datas_literal()
        for entry in entries:
            assert ".gguf" not in entry
            assert "qwentts" not in entry.lower()
            assert "packs" not in entry

    def test_the_managed_install_manifests_ship_as_package_data(self) -> None:
        # ``collect_submodules`` never reaches these JSON files, and the
        # loaders read them via ``Path(__file__).with_name(...)`` — a frozen
        # app without them reports every managed install as unshipped.
        text = SPEC_PATH.read_text(encoding="utf-8")
        for name in (
            "qwen_gguf_runtime_manifests.json",
            "qwen_gguf_model_manifests.json",
            # The official manifests share the same loader pattern — a GGUF
            # track must not leave the official install unresolvable frozen.
            "qwen_runtime_manifests.json",
            "qwen_model_manifests.json",
        ):
            assert name in text, f"spec does not bundle {name}"
            assert (PROJECT_ROOT / "src" / "vienetts_app" / "core" / name).is_file()


class TestGgufHostRealProcess:
    """The real module — what a frozen re-dispatch actually runs."""

    def test_the_host_announces_hello_and_exits_cleanly(self) -> None:
        from vienetts_app.core.qwen_gguf_engine import GGUF_HOST_MODULE
        from vienetts_app.core.qwen_protocol import read_frame

        proc = subprocess.run(
            [sys.executable, "-m", GGUF_HOST_MODULE],
            input=b"",
            capture_output=True,
            timeout=120,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == 0, stderr
        events = [
            json.loads(line)["event"]
            for line in stderr.splitlines()
            if line.strip().startswith("{")
        ]
        assert events[0] == "starting"
        frame = read_frame(BytesIO(proc.stdout))
        assert frame.type == "hello"

    def test_a_missing_library_is_a_recoverable_error_not_a_crash(self, tmp_path: Path) -> None:
        """A load pointing at a nonexistent pack must emit a structured
        error frame (recoverable — the GUI offers the install again), and
        the host must keep serving: a following shutdown exits 0."""
        from vienetts_app.core.qwen_gguf_engine import GGUF_HOST_MODULE
        from vienetts_app.core.qwen_protocol import (
            RUNTIME_INCOMPLETE_CODE,
            Frame,
            read_frame,
            write_frame,
        )

        proc = subprocess.Popen(
            [sys.executable, "-m", GGUF_HOST_MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            assert read_frame(proc.stdout).type == "hello"
            write_frame(
                proc.stdin,
                Frame(
                    type="load",
                    fields={
                        "profile": "customvoice",
                        "format": "gguf",
                        "quantization": "Q8_0",
                        "device": "cpu",
                        "runtimeDir": str(tmp_path / "no-such-pack"),
                        "talkerPath": str(tmp_path / "talker.gguf"),
                        "codecPath": str(tmp_path / "codec.gguf"),
                    },
                ),
            )
            proc.stdin.flush()
            error = read_frame(proc.stdout)
            assert error.type == "error"
            assert error.fields.get("fatal") is False
            assert error.fields.get("code") == RUNTIME_INCOMPLETE_CODE
            # Recoverable means the host is still alive and obedient.
            write_frame(proc.stdin, Frame(type="shutdown"))
            proc.stdin.flush()
            assert proc.wait(timeout=30) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


class TestGgufReleaseWorkflow:
    def test_the_frozen_gguf_host_is_probed_without_packs(self) -> None:
        # The release must prove the packaged binary can become the GGUF
        # host — the probe needs no runtime pack, so it runs in every job.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        assert '"--qwen-gguf-host"' in workflow or "GGUF_HOST_FLAG" in workflow
        assert "--qwen-gguf-host" in workflow
