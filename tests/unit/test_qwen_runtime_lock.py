"""Tests for the maintainer-only Qwen runtime lock generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vienetts_app.core import qwen_runtime_manifest as qm

SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "lock_qwen_runtime.py"


def load_lock_script():
    spec = importlib.util.spec_from_file_location("lock_qwen_runtime", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


LOCK_TEXT = """
torch==2.8.0+cpu \\
    --hash=sha256:{torch_hash} \\
    --hash=sha256:{other_hash}
torchaudio==2.8.0+cpu \\
    --hash=sha256:{audio_hash}
transformers==4.57.3 \\
    --hash=sha256:{transformers_hash}
qwen-tts==0.1.1 \\
    --hash=sha256:{qwen_hash}
sox==1.5.0 \\
    --hash=sha256:{sox_hash}
""".format(
    torch_hash="1" * 64,
    other_hash="2" * 64,
    audio_hash="3" * 64,
    transformers_hash="6" * 64,
    qwen_hash="4" * 64,
    sox_hash="5" * 64,
)

TORCH_HTML = (
    '<a href="https://download-r2.pytorch.org/whl/cpu/'
    'torch-2.8.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl#sha256={other}">'
    "torch-2.8.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl</a>"
    '<a href="https://download-r2.pytorch.org/whl/cpu/'
    'torch-2.8.0%2Bcpu-cp313-cp313-manylinux_2_28_x86_64.whl#sha256={wanted}">'
    "torch-2.8.0+cpu-cp313-cp313-manylinux_2_28_x86_64.whl</a>"
    '<a href="https://download-r2.pytorch.org/whl/cpu/'
    'torchaudio-2.8.0%2Bcpu-cp313-cp313-manylinux_2_28_x86_64.whl#sha256={audio}">'
    "torchaudio-2.8.0+cpu-cp313-cp313-manylinux_2_28_x86_64.whl</a>"
).format(other="2" * 64, wanted="1" * 64, audio="3" * 64)


def test_parse_uv_lock_reads_pins_and_hashes() -> None:
    lock = load_lock_script()
    pins = lock.parse_uv_lock(LOCK_TEXT)

    assert sorted(pins) == ["qwen-tts", "sox", "torch", "torchaudio", "transformers"]
    assert pins["torch"].version == "2.8.0+cpu"
    assert pins["torch"].hashes == frozenset({"1" * 64, "2" * 64})


def test_parse_uv_lock_rejects_a_pin_without_hashes() -> None:
    lock = load_lock_script()
    with pytest.raises(lock.LockError, match="no hashes"):
        lock.parse_uv_lock("torch==2.8.0+cpu\n")


def test_select_artifact_uses_the_resolver_digest_set() -> None:
    lock = load_lock_script()
    files = (
        lock.ArtifactFile(
            "torch-2.8.0+cpu-cp313-cp313-manylinux_2_28_x86_64.whl",
            "https://download.pytorch.org/whl/cpu/torch-2.8.0%2Bcpu-cp313-cp313-manylinux_2_28_x86_64.whl",
            "1" * 64,
        ),
        lock.ArtifactFile(
            "torch-2.8.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl",
            "https://download.pytorch.org/whl/cpu/torch-2.8.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl",
            "2" * 64,
        ),
    )

    selected = lock.select_artifact(files, frozenset({"1" * 64}), "cp313", "linux_x86_64")

    assert selected is not None
    assert selected.sha256 == "1" * 64

    # A digest the resolver never reported is never pinned, even if it is the
    # only candidate for this platform: fail closed instead of trusting it.
    assert lock.select_artifact(files, frozenset({"9" * 64}), "cp313", "linux_x86_64") is None


def test_select_artifact_rejects_non_allowlisted_and_ambiguous_wheels() -> None:
    lock = load_lock_script()
    foreign = lock.ArtifactFile(
        "demo-1.0-py3-none-any.whl",
        "https://example.invalid/packages/demo-1.0-py3-none-any.whl",
        "1" * 64,
    )
    with pytest.raises(lock.LockError, match="allowlisted"):
        lock.select_artifact((foreign,), frozenset({"1" * 64}), "cp313", "linux_x86_64")

    ambiguous = (
        lock.ArtifactFile(
            "demo-1.0-py3-none-any.whl",
            "https://files.pythonhosted.org/packages/a/demo-1.0-py3-none-any.whl",
            "1" * 64,
        ),
        lock.ArtifactFile(
            "demo-1.0-py2.py3-none-any.whl",
            "https://files.pythonhosted.org/packages/b/demo-1.0-py2.py3-none-any.whl",
            "1" * 64,
        ),
    )
    with pytest.raises(lock.LockError, match="ambiguous"):
        lock.select_artifact(ambiguous, frozenset({"1" * 64}), "cp313", "linux_x86_64")


def test_free_threaded_and_wrong_platform_wheels_are_not_selected() -> None:
    lock = load_lock_script()
    freethreaded = lock.ArtifactFile(
        "torch-2.8.0-cp313-cp313t-macosx_14_0_arm64.whl",
        "https://files.pythonhosted.org/packages/a/torch-2.8.0-cp313-cp313t-macosx_14_0_arm64.whl",
        "1" * 64,
    )
    assert (
        lock.select_artifact((freethreaded,), frozenset({"1" * 64}), "cp313", "macosx_11_0_arm64")
        is None
    )

    windows_only = lock.ArtifactFile(
        "demo-1.0-cp313-cp313-win_amd64.whl",
        "https://files.pythonhosted.org/packages/a/demo-1.0-cp313-cp313-win_amd64.whl",
        "1" * 64,
    )
    assert (
        lock.select_artifact((windows_only,), frozenset({"1" * 64}), "cp313", "linux_x86_64")
        is None
    )


def test_macos_prefers_the_lowest_deployment_target() -> None:
    lock = load_lock_script()
    files = tuple(
        lock.ArtifactFile(
            f"scipy-1.18.1-cp313-cp313-{tag}_arm64.whl",
            f"https://files.pythonhosted.org/packages/a/scipy-1.18.1-cp313-cp313-{tag}_arm64.whl",
            "1" * 64,
        )
        for tag in ("macosx_12_0", "macosx_14_0")
    )

    selected = lock.select_artifact(files, frozenset({"1" * 64}), "cp313", "macosx_11_0_arm64")

    assert selected is not None
    assert "macosx_12_0" in selected.filename


def test_index_files_uses_the_index_host_and_rejects_other_paths() -> None:
    lock = load_lock_script()
    files = lock.index_files(
        "https://download.pytorch.org/whl/cpu",
        "torch",
        fetch_text=lambda _url: TORCH_HTML,
    )

    assert [file.url for file in files] == [
        "https://download.pytorch.org/whl/cpu/torch-2.8.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl",
        "https://download.pytorch.org/whl/cpu/torch-2.8.0%2Bcpu-cp313-cp313-manylinux_2_28_x86_64.whl",
        "https://download.pytorch.org/whl/cpu/torchaudio-2.8.0%2Bcpu-cp313-cp313-manylinux_2_28_x86_64.whl",
    ]
    assert (
        lock.index_files(
            "https://download.pytorch.org/whl/cpu",
            "torch",
            fetch_text=lambda _url: (
                '<a href="https://x/other/torch.whl#sha256=' + "1" * 64 + '">t</a>'
            ),
        )
        == ()
    )
    with pytest.raises(lock.LockError, match="not allowlisted"):
        lock.index_files("https://mirror.invalid/whl/cpu", "torch", fetch_text=lambda _url: "")


def test_render_manifests_produces_a_manifest_the_app_accepts() -> None:
    lock = load_lock_script()
    requirements = {
        "indexes": {
            "pypi": "https://pypi.org/simple",
            "torchCpu": "https://download.pytorch.org/whl/cpu",
        },
        "platforms": [
            {
                "key": "linux-x64-cpu",
                "platformTag": "linux_x86_64",
                "device": "cpu",
                "index": "torchCpu",
                "requirements": ["qwen-tts==0.1.1", "torch==2.8.0+cpu"],
            }
        ],
    }
    pypi_payload = {
        "urls": [
            {
                "filename": "qwen_tts-0.1.1-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/packages/a/qwen_tts-0.1.1-py3-none-any.whl",
                "size": 4096,
                "digests": {"sha256": "4" * 64},
            },
            {
                "filename": "transformers-4.57.3-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/packages/a/transformers-4.57.3-py3-none-any.whl",
                "size": 2048,
                "digests": {"sha256": "6" * 64},
            },
            {
                "filename": "sox-1.5.0.tar.gz",
                "url": "https://files.pythonhosted.org/packages/a/sox-1.5.0.tar.gz",
                "size": 1024,
                "digests": {"sha256": "5" * 64},
            },
        ]
    }

    rendered = lock.render_manifests(
        requirements,
        python_version="3.13",
        platforms=requirements["platforms"],
        resolver=lambda _entry, _version: LOCK_TEXT,
        fetch_json=lambda _url: pypi_payload,
        fetch_text=lambda _url: TORCH_HTML,
        fetch_size=lambda _url: 7_000_000_000,
    )

    manifests = qm.load_manifests_from_data(rendered)
    manifest = manifests["linux-x64-cpu"]
    assert manifest.torch_local_version == "2.8.0+cpu"
    torch_wheel = manifest.wheel_for("torch")
    assert torch_wheel is not None
    assert torch_wheel.url.startswith("https://download.pytorch.org/whl/cpu/")
    assert torch_wheel.size_bytes == 7_000_000_000
    assert [record.name for record in manifest.sdist_only] == ["sox"]
    assert "wheel-only" in manifest.sdist_only[0].reason


def test_main_reports_missing_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = load_lock_script()
    monkeypatch.setattr(lock.shutil, "which", lambda _name: None)
    output = tmp_path / "manifests.json"

    assert lock.main(["--output", str(output)]) == 2
    assert not output.exists()


def test_check_mode_detects_a_stale_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = load_lock_script()
    requirements_path = tmp_path / "requirements.json"
    requirements_path.write_text(
        json.dumps(
            {
                "indexes": {"pypi": "https://pypi.org/simple"},
                "platforms": [
                    {
                        "key": "linux-x64-cpu",
                        "platformTag": "linux_x86_64",
                        "device": "cpu",
                        "index": "pypi",
                        "requirements": ["qwen-tts==0.1.1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifests.json"
    output.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(lock.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        lock,
        "render_manifests",
        lambda *_args, **_kwargs: {"formatVersion": lock.FORMAT_VERSION, "platforms": {}},
    )

    argv = ["--requirements", str(requirements_path), "--output", str(output), "--check"]
    assert lock.main(argv) == 1
    assert output.read_text(encoding="utf-8") == "stale"
