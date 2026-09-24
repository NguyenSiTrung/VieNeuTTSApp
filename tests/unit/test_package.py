"""Packaging sanity: the app package, the frozen bundle contract, and the
Qwen host's frozen re-dispatch (Phase 7 Task 7.1).

The spec file is parsed (never executed) so the bundle contract is pinned
wherever the suite runs: which modules are excluded, which data trees ship,
and that the host's runtime-only imports stay out of the app.
"""

import ast
import json
import os
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

from vienetts_app.__main__ import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "packaging" / "vienetts-app.spec"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
HOST_MODULE_PATH = PROJECT_ROOT / "src" / "vienetts_app" / "workers" / "qwen_host.py"


def test_release_version_matches_metadata_and_cli_fallback(monkeypatch, capsys) -> None:
    """The source-checkout --version fallback must match release metadata."""
    import vienetts_app
    from vienetts_app import _version

    pyproject_version = re.search(
        r'^version = "([^"]+)"$',
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    lock_version = re.search(
        r'\[\[package\]\]\nname = "vienetts-app"\nversion = "([^"]+)"',
        (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"),
    )

    assert pyproject_version is not None
    assert lock_version is not None
    expected_version = "0.2.0"
    assert pyproject_version.group(1) == expected_version
    assert lock_version.group(1) == expected_version
    assert vienetts_app.__version__ == expected_version

    monkeypatch.setattr(_version, "BUILD_VERSION", "")
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == vienetts_app.__version__


# --------------------------------------------------------------------------- #
# the frozen host re-dispatch
# --------------------------------------------------------------------------- #


class TestFrozenHostCommand:
    def test_a_source_checkout_runs_the_host_module(self) -> None:
        from vienetts_app.core.qwen_engine import HOST_MODULE, host_command

        assert host_command() == [sys.executable, "-m", HOST_MODULE]

    def test_a_frozen_build_redispatches_the_packaged_executable(self, monkeypatch) -> None:
        from vienetts_app.core.qwen_engine import HOST_FLAG, host_command, is_frozen

        assert is_frozen() is False
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert is_frozen() is True
        # A frozen build has no `python -m` to hand a module name to: the same
        # executable becomes the host, so the bundle's host half is what runs.
        assert host_command() == [sys.executable, HOST_FLAG]

    def test_the_cli_routes_the_host_flag_without_reaching_the_gui(self, monkeypatch) -> None:
        from vienetts_app.core.qwen_engine import HOST_FLAG
        from vienetts_app.workers import qwen_host

        calls: list[str] = []

        def fake_host_main() -> int:
            calls.append("host")
            return 7

        monkeypatch.setattr(qwen_host, "main", fake_host_main)
        assert main([HOST_FLAG]) == 7
        assert calls == ["host"]

    def test_the_cli_still_dispatches_the_gui_without_the_flag(self) -> None:
        assert main([], gui_runner=lambda: 3) == 3

    def test_the_host_flag_is_routed_before_the_gui_stdio_setup(self, monkeypatch) -> None:
        # stdout is the frame channel: the windowed-exe stdio safety net must
        # never touch it (nor install the GUI crash handler).
        import vienetts_app
        from vienetts_app.core.qwen_engine import HOST_FLAG
        from vienetts_app.workers import qwen_host

        touched: list[str] = []
        monkeypatch.setattr(vienetts_app, "ensure_windowed_stdio", lambda: touched.append("stdio"))
        monkeypatch.setattr(qwen_host, "main", lambda: 0)
        assert main([HOST_FLAG]) == 0
        assert touched == []

    def test_the_windows_spawn_is_windowless(self, tmp_path: Path, monkeypatch) -> None:
        # A console=False app must never flash a console for the host child.
        from vienetts_app.core import qwen_engine as qwen_engine_module
        from vienetts_app.core.qwen_engine import QwenEngine, QwenEngineError

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
        engine = QwenEngine(
            profile="qwen_custom_0_6b",
            model_dir=tmp_path / "model",
            shared_dir=tmp_path / "shared",
        )
        with pytest.raises(QwenEngineError):
            engine.initialize()
        assert recorded["creationflags"] == 0x08000000
        assert recorded["shell"] is False
        assert recorded["command"] == [sys.executable, "-m", "vienetts_app.workers.qwen_host"]


class TestFrozenHostEnvironment:
    def test_the_runtime_directory_travels_for_a_frozen_host(self, tmp_path: Path) -> None:
        from vienetts_app.core.qwen_engine import RUNTIME_ENV, host_environment

        runtime_dir = tmp_path / "khoảng cách" / "mô hình runtime"
        environment = host_environment(runtime_dir, {"PATH": "/usr/bin"})
        # PYTHONPATH serves a source checkout; a frozen host cannot use it and
        # reads this instead (workers.qwen_host.configure_import_path).
        assert environment[RUNTIME_ENV] == str(runtime_dir)
        assert str(runtime_dir) in environment["PYTHONPATH"].split(os.pathsep)
        assert environment["HF_HUB_OFFLINE"] == "1"
        assert environment["TRANSFORMERS_OFFLINE"] == "1"

    def test_without_a_runtime_dir_there_is_no_runtime_entry(self) -> None:
        from vienetts_app.core.qwen_engine import RUNTIME_ENV, host_environment

        assert RUNTIME_ENV not in host_environment(None, {"PATH": "/usr/bin"})


class TestHostRuntimeImportPath:
    """The host owns its import path: a frozen host cannot use PYTHONPATH."""

    def test_the_runtime_directory_is_inserted_first(self, tmp_path: Path) -> None:
        from vienetts_app.core.qwen_engine import RUNTIME_ENV

        runtime_dir = tmp_path / "khoảng cách" / "mô hình runtime"
        runtime_dir.mkdir(parents=True)
        code = (
            "import sys;"
            "from vienetts_app.workers.qwen_host import configure_import_path;"
            "added = configure_import_path();"
            "print(added[0] if added else '')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            # The printed path is non-ASCII: without this, a Windows child
            # encodes its stdout with the ANSI code page and dies (cp1252).
            env={
                **os.environ,
                RUNTIME_ENV: str(runtime_dir),
                "PYTHONIOENCODING": "utf-8",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(runtime_dir)

    def test_the_host_starts_from_a_runtime_path_with_spaces_and_non_ascii(
        self, tmp_path: Path
    ) -> None:
        # Paths with spaces/non-ASCII must survive the env → sys.path trip and
        # the spawn itself (shell=False, list argv).
        from vienetts_app.core.qwen_engine import HOST_MODULE, host_environment
        from vienetts_app.core.qwen_protocol import read_frame

        runtime_dir = tmp_path / "khoảng cách" / "mô hình runtime"
        runtime_dir.mkdir(parents=True)
        proc = subprocess.run(
            [sys.executable, "-m", HOST_MODULE],
            env=host_environment(runtime_dir, dict(os.environ)),
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
        assert events[-1] == "peer_closed"
        assert read_frame(BytesIO(proc.stdout)).type == "hello"


# --------------------------------------------------------------------------- #
# the frozen bundle contract
# --------------------------------------------------------------------------- #


def _spec_call(name: str) -> ast.Call:
    for node in ast.walk(ast.parse(SPEC_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == name:
            return node
    raise AssertionError(f"the spec has no {name}() call")


def _spec_keyword(call: ast.Call, name: str) -> ast.expr:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"the spec's {getattr(call.func, 'id', '?')}() has no {name}=")


def _host_imports(*, module_level: bool) -> set[str]:
    """Top-level modules the host imports at module level (or, deferred, only
    inside functions/classes — the managed-runtime-only stack)."""
    tree = ast.parse(HOST_MODULE_PATH.read_text(encoding="utf-8"))
    nested = [node for node in tree.body if isinstance(node, ast.ClassDef | ast.FunctionDef)]
    scopes = [node for node in tree.body if node not in nested] if module_level else nested
    collected: set[str] = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Import):
                collected |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                collected.add(node.module.split(".")[0])
    return collected - (
        {n for n in _host_imports(module_level=True)} if not module_level else set()
    )


def _spec_datas_literal() -> list[str]:
    """The literal ``datas = [...]`` entries — what the spec ships from the
    repo (later ``datas +=`` lines add package data from site-packages)."""
    for node in ast.walk(ast.parse(SPEC_PATH.read_text(encoding="utf-8"))):
        targets = getattr(node, "targets", [])
        if (
            isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") == "datas" for target in targets)
            and isinstance(node.value, ast.List)
        ):
            return [ast.unparse(element) for element in node.value.elts]
    raise AssertionError("the spec has no literal `datas = [...]` block")


def _third_party(names: set[str]) -> set[str]:
    return {
        name for name in names if name not in sys.stdlib_module_names and name != "vienetts_app"
    }


class TestSpecContract:
    def test_every_runtime_only_host_import_is_excluded(self) -> None:
        # The host's heavy imports (torch, qwen_tts) are deferred to the first
        # `load` frame and come from the managed runtime. A new heavy import
        # must join the spec's excludes — otherwise a build machine with the
        # `gpu` extra would silently bundle the Qwen stack.
        excluded = {
            constant.value
            for constant in _spec_keyword(_spec_call("Analysis"), "excludes").elts
            if isinstance(constant, ast.Constant)
        }
        runtime_only = _third_party(_host_imports(module_level=False))
        assert runtime_only, "the host no longer defers any import — re-check the spec"
        assert runtime_only <= excluded, f"not excluded from the bundle: {runtime_only - excluded}"
        # …while what the host DOES import at module level stays bundled.
        bundled = _third_party(_host_imports(module_level=True))
        assert bundled  # numpy, at least
        assert not (bundled & excluded), f"bundled imports are excluded: {bundled & excluded}"

    def test_the_host_and_protocol_modules_ship_explicitly(self) -> None:
        text = SPEC_PATH.read_text(encoding="utf-8")
        assert '"vienetts_app.workers.qwen_host"' in text
        assert '"vienetts_app.core.qwen_protocol"' in text

    def test_bundled_data_stays_inside_the_app_package(self) -> None:
        # app.py resolves QML/assets relative to the package; a data tree
        # landing anywhere else needs frozen-mode code paths. The managed Qwen
        # runtime and the model weights are never bundled — the app downloads
        # them into its data dir on demand.
        entries = _spec_datas_literal()
        assert entries, "the spec bundles no data trees"
        for entry in entries:
            assert re.match(r"^\(str\(REPO / ['\"]src/vienetts_app/", entry), entry

    def test_the_gui_is_windowless_and_the_macos_bundle_is_signed(self) -> None:
        assert _spec_keyword(_spec_call("EXE"), "console").value is False
        plist = _spec_keyword(_spec_call("BUNDLE"), "info_plist")
        assert isinstance(plist, ast.Dict)
        keys = {key.value for key in plist.keys if isinstance(key, ast.Constant)}
        assert {"CFBundleDisplayName", "CFBundleShortVersionString"} <= keys
        # The ad-hoc signature must cover the nested binaries the host
        # re-dispatch spawns, so the workflow signs the whole bundle.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        assert "codesign --force --deep --sign - dist/VieNeuTTS.app" in workflow


class TestReleaseWorkflowHostContract:
    def test_the_frozen_host_is_probed_without_models(self) -> None:
        # A release must prove the packaged binary can become the host: the
        # probe needs no runtime and no weights, so it runs in every job.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        assert '"--qwen-host"' in workflow
        assert "read_frame" in workflow

    def test_the_bundle_is_asserted_free_of_the_qwen_stack(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        assert "-name qwen_tts" in workflow
        assert "-name torch" in workflow
