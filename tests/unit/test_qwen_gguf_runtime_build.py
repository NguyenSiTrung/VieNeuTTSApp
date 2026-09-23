"""Build/lock tooling tests for the qwentts.cpp GGUF runtime packs.

These tests pin the *commands and inventory* of the native pack tooling —
they never run cmake, clone repositories or load native libraries. The real
pack evidence per cell lives under ``docs/performance/evidence/`` next to the
probe JSONs (Task 1.1/6.2).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_qwen_gguf_runtime as build  # noqa: E402
import lock_qwen_gguf_runtime as lock  # noqa: E402

REQUIREMENTS_PATH = REPO_ROOT / "packaging" / "qwen-gguf-runtime-requirements.json"
PINNED_COMMIT = "0cbde9b5d21aa2142efa5d62e14b97f197ac6f6d"
PINNED_GGML = "0af0d7d5f66a6976b259b292cb4e7dc60457aa45"

CELLS = {
    "windows-x64-cpu": "cpu",
    "windows-x64-cuda": "cuda",
    "linux-x64-cpu": "cpu",
    "linux-x64-cuda": "cuda",
    "macos-arm64-cpu": "cpu",
    "macos-arm64-metal": "metal",
}


@pytest.fixture(scope="module")
def requirements() -> dict:
    return json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))


def _populate_pack(pack: Path, spec: dict, *, with_licenses: bool = True) -> None:
    """Materialize a minimal pack satisfying the cell's inventory rules."""
    pack.mkdir(parents=True, exist_ok=True)
    for rel in build.required_pack_files(spec):
        target = pack / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"fake-{rel}".encode())
    for rule in build.pack_globs(spec):
        target = pack / rule["pattern"].replace("*", "fake")
        if not target.exists():
            target.write_bytes(f"fake-{target.name}".encode())
    if with_licenses:
        lic = pack / "licenses"
        lic.mkdir(exist_ok=True)
        (lic / "qwentts.cpp-MIT.txt").write_text("MIT license text")
        (lic / "ggml-MIT.txt").write_text("MIT license text")


class TestCellSpec:
    def test_every_cell_resolves_from_requirements(self, requirements) -> None:
        for key, device in CELLS.items():
            spec = build.cell_spec(requirements, key)
            assert spec["key"] == key
            assert spec["device"] == device
            assert spec["ggmlBackend"]

    def test_unknown_cell_rejected(self, requirements) -> None:
        with pytest.raises(build.BuildError):
            build.cell_spec(requirements, "linux-arm64-cpu")

    def test_every_cell_declares_a_deployment_floor(self, requirements) -> None:
        for key in CELLS:
            spec = build.cell_spec(requirements, key)
            floor = spec.get("deploymentFloor")
            assert isinstance(floor, str) and floor.strip(), key


class TestClonePlan:
    def test_pins_repo_and_submodule_commits(self, requirements) -> None:
        plan = build.clone_plan(requirements["upstream"]["runtime"], Path("/tmp/src"))
        flat = [" ".join(cmd) for cmd in plan]
        joined = "\n".join(flat)
        assert PINNED_COMMIT in joined
        assert PINNED_GGML in joined
        assert any(cmd[:2] == ["git", "clone"] for cmd in plan)
        assert any("submodule" in cmd for cmd in flat)

    def test_never_follows_head(self, requirements) -> None:
        plan = build.clone_plan(requirements["upstream"]["runtime"], Path("/tmp/src"))
        joined = "\n".join(" ".join(cmd) for cmd in plan)
        assert "HEAD" not in joined
        assert "--depth" not in joined  # pinned commits need full object access


class TestConfigureCommand:
    @pytest.mark.parametrize("cell", sorted(CELLS))
    def test_shared_library_and_backend_dl_always_on(self, requirements, cell) -> None:
        spec = build.cell_spec(requirements, cell)
        cmd = build.configure_command(spec, Path("/src"), Path("/build"))
        assert "-DQWEN_SHARED=ON" in cmd
        assert "-DGGML_BACKEND_DL=ON" in cmd
        assert "-DGGML_CPU_ALL_VARIANTS=ON" in cmd
        assert "-DCMAKE_BUILD_TYPE=Release" in cmd

    @pytest.mark.parametrize("cell", ["windows-x64-cuda", "linux-x64-cuda"])
    def test_cuda_cells_enable_cuda_backend(self, requirements, cell) -> None:
        spec = build.cell_spec(requirements, cell)
        cmd = build.configure_command(spec, Path("/src"), Path("/build"))
        assert "-DGGML_CUDA=ON" in cmd

    def test_metal_cell_enables_metal_backend(self, requirements) -> None:
        spec = build.cell_spec(requirements, "macos-arm64-metal")
        cmd = build.configure_command(spec, Path("/src"), Path("/build"))
        assert "-DGGML_METAL=ON" in cmd

    @pytest.mark.parametrize("cell", ["windows-x64-cpu", "linux-x64-cpu", "macos-arm64-cpu"])
    def test_cpu_cells_enable_no_gpu_backend(self, requirements, cell) -> None:
        spec = build.cell_spec(requirements, cell)
        cmd = build.configure_command(spec, Path("/src"), Path("/build"))
        assert "-DGGML_CUDA=ON" not in cmd
        assert "-DGGML_METAL=ON" not in cmd

    def test_vulkan_never_enabled(self, requirements) -> None:
        for key in CELLS:
            spec = build.cell_spec(requirements, key)
            cmd = build.configure_command(spec, Path("/src"), Path("/build"))
            assert not any("VULKAN" in arg.upper() for arg in cmd), key

    def test_source_and_build_dirs_come_from_arguments(self, requirements) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        cmd = build.configure_command(spec, Path("/opt/qwentts"), Path("/opt/build"))
        assert "/opt/qwentts" in cmd
        assert "/opt/build" in cmd


class TestBuildCommand:
    def test_release_config_and_parallel(self, requirements) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        cmd = build.build_command(spec, Path("/build"), jobs=8)
        assert cmd[:2] == ["cmake", "--build"]
        assert "/build" in cmd
        assert "Release" in cmd
        assert "8" in cmd


class TestPackInventory:
    @pytest.mark.parametrize("cell", sorted(CELLS))
    def test_required_files_include_runtime_and_license(self, requirements, cell) -> None:
        spec = build.cell_spec(requirements, cell)
        files = build.required_pack_files(spec)
        assert files, cell
        assert any("qwen" in f for f in files), cell

    def test_linux_inventory_matches_verified_build(self, requirements) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        files = build.required_pack_files(spec)
        assert "libqwen.so" in files
        assert "libggml.so.0" in files or "libggml.so" in files
        assert any(f.startswith("libggml-base") for f in files)

    def test_windows_uses_dll_names(self, requirements) -> None:
        spec = build.cell_spec(requirements, "windows-x64-cpu")
        files = build.required_pack_files(spec)
        assert any(f.endswith(".dll") for f in files)
        assert not any(f.endswith(".so") or ".so." in f for f in files)

    def test_macos_inventory_matches_verified_build(self, requirements) -> None:
        # Verified on arm64 (macos-arm64-metal build): shared libs are
        # .dylib, backend modules are CMake MODULE .so — and there is no
        # generic libggml-cpu, only ISA-variant modules via pack_globs.
        spec = build.cell_spec(requirements, "macos-arm64-cpu")
        files = build.required_pack_files(spec)
        for name in ("libqwen.dylib", "libggml.dylib", "libggml-base.dylib"):
            assert name in files
        assert not any(f.endswith(".so") for f in files)
        spec = build.cell_spec(requirements, "macos-arm64-metal")
        assert "libggml-metal.so" in build.required_pack_files(spec)

    def test_macos_pack_verifies_with_cpu_variant_module(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "macos-arm64-metal")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        assert build.verify_pack(pack, spec) == []


class TestVerifyPack:
    def test_complete_pack_passes(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        assert build.verify_pack(pack, spec) == []

    def test_missing_runtime_library_rejects(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        (pack / "libqwen.so").unlink()
        problems = build.verify_pack(pack, spec)
        assert problems
        assert any("libqwen" in p for p in problems)

    def test_missing_license_rejects(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        for lic in (pack / "licenses").iterdir():
            lic.unlink()
        problems = build.verify_pack(pack, spec)
        assert any("license" in p.lower() or "licenses" in p.lower() for p in problems)

    def test_missing_cpu_backend_module_rejects(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        for f in pack.glob("libggml-cpu-*"):
            f.unlink()
        problems = build.verify_pack(pack, spec)
        assert problems

    def test_cuda_cell_requires_cuda_module(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cuda")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        cuda_mods = [f for f in pack.iterdir() if "cuda" in f.name.lower()]
        assert cuda_mods, "fixture should have created a cuda module"
        for f in cuda_mods:
            f.unlink()
        problems = build.verify_pack(pack, spec)
        assert problems


class TestLockManifest:
    def test_manifest_records_files_with_digests(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        manifest = lock.pack_manifest(pack, spec, requirements["upstream"]["runtime"])
        assert manifest["cell"] == "linux-x64-cpu"
        assert manifest["abiVersion"] == 5
        assert manifest["upstream"]["commit"] == PINNED_COMMIT
        assert manifest["upstream"]["ggmlSubmoduleCommit"] == PINNED_GGML
        paths = {f["path"] for f in manifest["files"]}
        assert "libqwen.so" in paths
        for entry in manifest["files"]:
            assert len(entry["sha256"]) == 64
            assert entry["size"] > 0

    def test_manifest_is_deterministic(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        a = lock.pack_manifest(pack, spec, requirements["upstream"]["runtime"])
        b = lock.pack_manifest(pack, spec, requirements["upstream"]["runtime"])
        assert a == b
        paths = [f["path"] for f in a["files"]]
        assert paths == sorted(paths)

    def test_manifest_digests_match_content(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        manifest = lock.pack_manifest(pack, spec, requirements["upstream"]["runtime"])
        for entry in manifest["files"]:
            digest = hashlib.sha256((pack / entry["path"]).read_bytes()).hexdigest()
            assert entry["sha256"] == digest

    def test_symlinks_recorded_as_links_not_digested(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        os.symlink("libggml.so.0.23.0", pack / "libggml.so")
        manifest = lock.pack_manifest(pack, spec, requirements["upstream"]["runtime"])
        links = {link["path"]: link["target"] for link in manifest.get("links", [])}
        assert links.get("libggml.so") == "libggml.so.0.23.0"
        assert "libggml.so" not in {f["path"] for f in manifest["files"]}

    def test_check_pack_detects_drift(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        manifest = lock.pack_manifest(pack, spec, requirements["upstream"]["runtime"])
        (pack / "libqwen.so").write_bytes(b"tampered")
        problems = lock.check_pack(pack, manifest)
        assert any("libqwen" in p for p in problems)

    def test_check_pack_detects_missing_file(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        pack = tmp_path / "pack"
        _populate_pack(pack, spec)
        manifest = lock.pack_manifest(pack, spec, requirements["upstream"]["runtime"])
        (pack / "libqwen.so").unlink()
        problems = lock.check_pack(pack, manifest)
        assert any("libqwen" in p for p in problems)


class TestGlibcFloor:
    def test_parses_max_version(self) -> None:
        text = (
            "0000 g DF .text 0000 GLIBC_2.34 malloc\n"
            "0000 g DF .text 0000 GLIBC_2.38 __isoc23_strtol\n"
            "0000 g DF .text 0000 GLIBC_2.17 free\n"
        )
        assert build._max_glibc_from_objdump(text) == "2.38"

    def test_no_glibc_refs_returns_none(self) -> None:
        assert build._max_glibc_from_objdump("0000 g DF .text 0000 foo\n") is None

    def test_minor_versions_compare_numerically(self) -> None:
        text = "x GLIBC_2.9 a\ny GLIBC_2.17 b\n"
        assert build._max_glibc_from_objdump(text) == "2.17"


class TestSmokeCommand:
    def test_symbol_smoke_loads_via_ctypes(self, requirements, tmp_path) -> None:
        spec = build.cell_spec(requirements, "linux-x64-cpu")
        cmd = build.symbol_smoke_command(spec, tmp_path / "pack")
        joined = " ".join(cmd)
        assert "ctypes" in joined
        assert "libqwen.so" in joined
        assert "qt_version" in joined
        # interpreter is invoked, never the library directly
        assert cmd[0] == sys.executable or "python" in Path(cmd[0]).name
