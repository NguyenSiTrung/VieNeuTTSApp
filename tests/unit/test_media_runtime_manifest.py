"""Media runtime pack publishing (FR-2.6): requirements → packs → lock → manifest.

``packaging/media-runtime-requirements.json`` pins every published pack
(macOS ``whisper-cli`` built from source, mirrored upstream ffmpeg builds);
``scripts/lock_media_runtime.py`` stages, probes, packs and locks them inside
the opt-in ``media-runtime-build.yml`` workflow, and
``scripts/render_media_runtime_manifests.py`` turns the lock into the shipped
``core/media_runtime_manifests.json``. Nothing here touches the network or
runs a real ffmpeg/whisper binary: upstream archives are synthesized locally
and native CLIs are pure-Python stubs.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import scripts.lock_media_runtime as lock
import scripts.render_media_runtime_manifests as render
import yaml

from vienetts_app.core import ai_models

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOWS_DIR / "media-runtime-build.yml"
BASE = "https://nguyensitrung.github.io/VieNeuTTSApp/packs"
WHISPER_HELP = (
    "usage: whisper-cli [options] file0 file1 ...\n"
    "  -mc N,     --max-context N  maximum number of text context tokens\n"
    "  -ojf,      --output-json-full  include more information in the JSON file\n"
    "  -ng,       --no-gpu  disable GPU\n"
    "             --vad  enable Voice Activity Detection (VAD)\n"
    "  -vm FNAME, --vad-model FNAME  VAD model path\n"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage(root: Path, spec: dict) -> Path:
    """A staged pack whose executables are inert placeholders."""
    stage = root / f"stage-{spec['runtime']}-{spec['cell']}"
    for rel in spec["binaries"]:
        exe = stage / rel
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"stub " + rel.encode())
    (stage / "LICENSE").write_text("license text\n", encoding="utf-8")
    return stage


def _probe_outputs(root: Path, spec: dict, **overrides: str) -> Path:
    """Probe transcripts that satisfy (or, via overrides, violate) the spec."""
    out = root / f"probe-{spec['runtime']}-{spec['cell']}"
    out.mkdir(parents=True, exist_ok=True)
    for probe in spec["probes"]:
        lines = list(probe.get("contains", []))
        lines += [f" V....D {name:<20} test entry" for name in probe.get("listed", [])]
        text = overrides.get(probe["name"], "\n".join(lines) + "\n")
        (out / f"{probe['name']}.txt").write_text(text, encoding="utf-8")
    return out


def _write_stub(path: Path, help_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!/usr/bin/env python3\nimport sys\nsys.stderr.write({help_text!r})\nsys.exit(0)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _locked_pack(root: Path, requirements: dict, runtime: str, cell: str, packs: Path) -> dict:
    spec = lock.cell_spec(requirements, runtime, cell)
    pack = lock.build_pack(requirements, spec, _stage(root, spec), root / "out")
    return lock.lock_pack(
        requirements,
        spec,
        pack,
        _probe_outputs(root, spec),
        root / "lock.json",
        publish_root=packs,
    )


def test_render_publishes_only_locked_cells_and_refuses_drift(tmp_path) -> None:
    requirements = lock.load_requirements()
    packs = tmp_path / "packs"
    _locked_pack(tmp_path, requirements, "whisper_cpp", "macos-arm64-metal", packs)
    _locked_pack(tmp_path, requirements, "ffmpeg", "windows-x64", packs)
    locked = json.loads((tmp_path / "lock.json").read_text(encoding="utf-8"))

    doc = render.render_manifest(requirements, locked, packs_dir=packs, downloads_base=BASE)

    # The app's own strict parser accepts the rendering, and ONLY the locked
    # cells carry a download recipe — every other cell is truthfully absent.
    manifest = ai_models.parse_media_runtime_manifest(doc)
    published = {
        (runtime, cell_id)
        for runtime in (ai_models.WHISPER_CPP_RUNTIME, ai_models.FFMPEG_RUNTIME)
        for cell_id, cell in manifest.cells(runtime).items()
        if cell.published
    }
    assert published == {("whisper_cpp", "macos-arm64-metal"), ("ffmpeg", "windows-x64")}
    assert set(manifest.cells("ffmpeg")) == set(ai_models.FFMPEG_CELLS)
    for runtime, cell_id in published:
        entry = locked["packs"][f"{runtime}/{cell_id}"]
        cell = manifest.cell(runtime, cell_id)
        on_pages = packs / entry["path"]
        assert cell.url == f"{BASE}/{entry['path']}"
        assert (cell.size_bytes, cell.sha256) == (on_pages.stat().st_size, _sha(on_pages))
        # Content-addressed: a rebuild can never replace bytes a shipped
        # manifest already pins.
        assert entry["sha256"][:12] in cell.url
        assert cell.url.endswith(f".{cell.archive}")
    assert manifest.cell("whisper_cpp", "macos-arm64-metal").archive == "tar.gz"
    assert manifest.cell("ffmpeg", "windows-x64").archive == "zip"

    # The CLI writes the shipped file, and --check reports no drift against it.
    out = tmp_path / "media_runtime_manifests.json"
    args = ["--lock", str(tmp_path / "lock.json"), "--packs-dir", str(packs), "--out", str(out)]
    assert render.main([*args, "--downloads-base", BASE]) == 0
    assert render.main([*args, "--downloads-base", BASE, "--check"]) == 0
    assert ai_models.load_media_runtime_manifest(out).cell("ffmpeg", "windows-x64").published
    out.write_text(
        out.read_text(encoding="utf-8").replace('"published": true', '"published": false', 1)
    )
    assert render.main([*args, "--downloads-base", BASE, "--check"]) == 1

    def refused(match: str, lock_doc: dict = locked, reqs: dict = requirements) -> None:
        with pytest.raises(render.RenderError, match=match):
            render.render_manifest(reqs, lock_doc, packs_dir=packs, downloads_base=BASE)

    # A locked cell the requirements do not declare is refused.
    bogus = copy.deepcopy(locked)
    bogus["packs"]["ffmpeg/windows-arm64"] = {
        **locked["packs"]["ffmpeg/windows-x64"],
        "cell": "windows-arm64",
    }
    refused("absent from the requirements", bogus)
    # The published bytes must be exactly the locked ones.
    on_pages = packs / locked["packs"]["whisper_cpp/macos-arm64-metal"]["path"]
    original = on_pages.read_bytes()
    on_pages.write_bytes(original + b"x")
    refused("size")
    on_pages.write_bytes(bytes([original[0] ^ 0x01]) + original[1:])
    refused("sha256")
    on_pages.unlink()
    refused("missing")
    on_pages.write_bytes(original)
    # Requirements that changed since the lock (stale) or carry an unfilled pin.
    stale = copy.deepcopy(requirements)
    stale["runtimes"]["whisper_cpp"]["cells"]["macos-arm64-metal"]["cmakeFlags"].append(
        "-DGGML_BLAS=OFF"
    )
    refused("stale", reqs=stale)
    placeholder = copy.deepcopy(requirements)
    windows = placeholder["runtimes"]["ffmpeg"]["cells"]["windows-x64"]
    windows["upstream"]["assets"][0]["sha256"] = "PIN-ME"
    refused("placeholder", reqs=placeholder)
    # Without a lock nothing is published at all.
    empty = render.render_manifest(requirements, {}, packs_dir=None, downloads_base=BASE)
    assert not any(
        cell["published"] for body in empty["runtimes"].values() for cell in body["cells"].values()
    )


def test_pack_and_lock_gate_layout_probes_and_licence(tmp_path) -> None:
    requirements = lock.load_requirements()
    spec = lock.cell_spec(requirements, "whisper_cpp", "macos-x64-cpu")
    stage = tmp_path / "stage"
    cli = stage / "bin" / "whisper-cli"
    _write_stub(cli, WHISPER_HELP)

    # No licence → no pack.
    with pytest.raises(lock.LockError, match="LICENSE"):
        lock.build_pack(requirements, spec, stage, tmp_path / "out")
    (stage / "LICENSE").write_text("MIT License\n", encoding="utf-8")

    # The smoke runs the staged CLI from a foreign cwd; ``--vad-model`` alone
    # does not satisfy the ``--vad`` flag.
    _write_stub(cli, WHISPER_HELP.replace("             --vad  enable", "  enable"))
    with pytest.raises(lock.LockError, match="--vad"):
        lock.run_probes(spec, stage, tmp_path / "probe-bad")
    _write_stub(cli, WHISPER_HELP)
    probe_dir = tmp_path / "probe"
    outputs = lock.run_probes(spec, stage, probe_dir)
    assert "--vad" in outputs["help"]

    pack = lock.build_pack(requirements, spec, stage, tmp_path / "out")
    entry = lock.lock_pack(requirements, spec, pack, probe_dir, tmp_path / "lock.json")
    assert (entry["size"], entry["sha256"]) == (pack.stat().st_size, _sha(pack))
    assert entry["path"].startswith("media/whisper_cpp/macos-x64-cpu/")
    with tarfile.open(pack) as tf:
        members = {m.name: m for m in tf.getmembers()}
        source = tf.extractfile(members["SOURCE.txt"]).read().decode("utf-8")
    assert {"bin/whisper-cli", "LICENSE", "SOURCE.txt"} <= set(members)
    assert members["bin/whisper-cli"].mode & stat.S_IXUSR
    assert spec["upstream"]["commit"] in source and "-DGGML_METAL=OFF" in source

    # A pack with a member escaping the install dir is refused.
    evil_dir = tmp_path / "evil"
    evil_dir.mkdir()
    evil = evil_dir / pack.name
    with tarfile.open(pack) as src, tarfile.open(evil, "w:gz") as dst:
        for member in src.getmembers():
            dst.addfile(member, src.extractfile(member))
        info = tarfile.TarInfo("../outside")
        info.size = 1
        dst.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(lock.LockError, match="outside"):
        lock.lock_pack(requirements, spec, evil, probe_dir, tmp_path / "lock.json")

    # ffmpeg: the probe transcripts are the licence and capability gate.
    fspec = lock.cell_spec(requirements, "ffmpeg", "linux-x64")
    fpack = lock.build_pack(requirements, fspec, _stage(tmp_path, fspec), tmp_path / "fout")
    good = _probe_outputs(tmp_path, fspec)
    version = (good / "ffmpeg-version.txt").read_text(encoding="utf-8")
    nonfree = _probe_outputs(
        tmp_path / "nonfree",
        fspec,
        **{
            "ffmpeg-version": version + "configuration: --enable-nonfree\n",
        },
    )
    with pytest.raises(lock.LockError, match="--enable-nonfree"):
        lock.lock_pack(requirements, fspec, fpack, nonfree, tmp_path / "lock.json")
    encoders = (good / "encoders.txt").read_text(encoding="utf-8")
    no_vp9 = _probe_outputs(
        tmp_path / "novp9", fspec, encoders=encoders.replace("libvpx-vp9", "libvpx")
    )
    with pytest.raises(lock.LockError, match="libvpx-vp9"):
        lock.lock_pack(requirements, fspec, fpack, no_vp9, tmp_path / "lock.json")
    (good / "filters.txt").unlink()
    with pytest.raises(lock.LockError, match="filters"):
        lock.lock_pack(requirements, fspec, fpack, good, tmp_path / "lock.json")
    # gh-pages is a git branch: a pack over GitHub's file limit never locks.
    capped = copy.deepcopy(requirements)
    capped["maxPackBytes"] = pack.stat().st_size - 1
    with pytest.raises(lock.LockError, match="limit"):
        lock.lock_pack(capped, spec, pack, probe_dir, tmp_path / "lock.json")


def _zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _pin_assets(spec: dict, downloads: Path, cell_cfg: dict) -> None:
    """Re-pin the cell's assets to the synthesized files + write upstream listings."""
    assets = cell_cfg["upstream"]["assets"]
    for asset in assets:
        path = downloads / asset["name"]
        asset["size"], asset["sha256"] = path.stat().st_size, _sha(path)
        if asset.get("checksumUrl"):
            listing = f"{'0' * 64}  unrelated.zip\n{asset['sha256']}  {asset['name']}\n"
            (downloads / f"{asset['name']}{lock.UPSTREAM_CHECKSUM_SUFFIX}").write_text(listing)


def test_upstream_ffmpeg_is_verified_against_pins_and_restaged(tmp_path) -> None:
    requirements = copy.deepcopy(lock.load_requirements())
    cells = requirements["runtimes"]["ffmpeg"]["cells"]

    # Windows: BtbN gpl-shared zip with a top dir; only the exes, their DLLs
    # and the licence are restaged (ffplay, docs and headers are dropped).
    win = tmp_path / "win"
    win.mkdir()
    win_cfg = cells["windows-x64"]
    top = win_cfg["upstream"]["assets"][0]["name"].removesuffix(".zip")
    _zip(
        win / win_cfg["upstream"]["assets"][0]["name"],
        {
            f"{top}/bin/ffmpeg.exe": b"MZ ffmpeg",
            f"{top}/bin/ffprobe.exe": b"MZ ffprobe",
            f"{top}/bin/ffplay.exe": b"MZ ffplay",
            f"{top}/bin/avcodec-62.dll": b"MZ avcodec",
            f"{top}/doc/ffmpeg.html": b"<html>",
            f"{top}/LICENSE.txt": b"GPLv3",
        },
    )
    _pin_assets(lock.cell_spec(requirements, "ffmpeg", "windows-x64"), win, win_cfg)
    spec = lock.cell_spec(requirements, "ffmpeg", "windows-x64")
    lock.verify_upstream(spec, win)
    lock.stage_upstream(spec, win, tmp_path / "win-stage")
    staged = {
        p.relative_to(tmp_path / "win-stage").as_posix()
        for p in (tmp_path / "win-stage").rglob("*")
        if p.is_file()
    }
    assert staged == {"bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/avcodec-62.dll", "LICENSE"}

    # Pinned bytes and the upstream checksum file must both agree.
    asset = win / win_cfg["upstream"]["assets"][0]["name"]
    good = asset.read_bytes()
    asset.write_bytes(good[:-1] + bytes([good[-1] ^ 0x01]))
    with pytest.raises(lock.LockError, match="pinned sha256"):
        lock.verify_upstream(spec, win)
    asset.write_bytes(good)
    listing = win / f"{asset.name}{lock.UPSTREAM_CHECKSUM_SUFFIX}"
    listing.write_text(f"{'f' * 64}  {asset.name}\n")
    with pytest.raises(lock.LockError, match="upstream checksum"):
        lock.verify_upstream(spec, win)
    listing.write_text(f"{'f' * 64}  another.zip\n")
    with pytest.raises(lock.LockError, match="does not list"):
        lock.verify_upstream(spec, win)

    # macOS: one zip per tool plus the FFmpeg licence text as a plain asset.
    mac = tmp_path / "mac"
    mac.mkdir()
    mac_cfg = cells["macos-arm64"]
    _zip(mac / "ffmpeg.zip", {"ffmpeg": b"\xcf\xfa\xed\xfe ffmpeg", "__MACOSX/._ffmpeg": b"rsrc"})
    _zip(mac / "ffprobe.zip", {"ffprobe": b"\xcf\xfa\xed\xfe ffprobe"})
    (mac / "COPYING.GPLv3").write_text("GNU GENERAL PUBLIC LICENSE\n", encoding="utf-8")
    _pin_assets(lock.cell_spec(requirements, "ffmpeg", "macos-arm64"), mac, mac_cfg)
    mspec = lock.cell_spec(requirements, "ffmpeg", "macos-arm64")
    lock.verify_upstream(mspec, mac)
    lock.stage_upstream(mspec, mac, tmp_path / "mac-stage")
    mac_stage = tmp_path / "mac-stage"
    assert (mac_stage / "LICENSE").read_text(encoding="utf-8").startswith("GNU GENERAL")
    assert sorted(p.name for p in (mac_stage / "bin").iterdir()) == ["ffmpeg", "ffprobe"]
    if os.name != "nt":
        assert (mac_stage / "bin" / "ffmpeg").stat().st_mode & stat.S_IXUSR

    if sys.platform == "win32":
        return  # the Linux pack keeps its soname symlinks; Windows cannot stage those
    # Linux: gpl-shared tar.xz; the soname symlinks survive staging and packing
    # (RUNPATH is $ORIGIN/../lib) while the dev ``.so`` link is dropped.
    lin = tmp_path / "lin"
    lin.mkdir()
    lin_cfg = cells["linux-x64"]
    name = lin_cfg["upstream"]["assets"][0]["name"]
    top = name.removesuffix(".tar.xz")
    with tarfile.open(lin / name, "w:xz") as tf:
        for member, data in {
            "bin/ffmpeg": b"\x7fELF ffmpeg",
            "bin/ffprobe": b"\x7fELF ffprobe",
            "lib/libavcodec.so.62.1.100": b"\x7fELF avcodec",
            "LICENSE.txt": b"GPLv3",
        }.items():
            info = tarfile.TarInfo(f"{top}/{member}")
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
        for link in ("libavcodec.so.62", "libavcodec.so"):
            info = tarfile.TarInfo(f"{top}/lib/{link}")
            info.type, info.linkname = tarfile.SYMTYPE, "libavcodec.so.62.1.100"
            tf.addfile(info)
    _pin_assets(lock.cell_spec(requirements, "ffmpeg", "linux-x64"), lin, lin_cfg)
    lspec = lock.cell_spec(requirements, "ffmpeg", "linux-x64")
    lock.verify_upstream(lspec, lin)
    lin_stage = tmp_path / "lin-stage"
    lock.stage_upstream(lspec, lin, lin_stage)
    assert sorted(p.name for p in (lin_stage / "lib").iterdir()) == [
        "libavcodec.so.62",
        "libavcodec.so.62.1.100",
    ]
    assert os.readlink(lin_stage / "lib" / "libavcodec.so.62") == "libavcodec.so.62.1.100"
    pack = lock.build_pack(requirements, lspec, lin_stage, tmp_path / "lin-out")
    assert pack.name.endswith(".tar.xz")
    with tarfile.open(pack) as tf:
        link = tf.getmember("lib/libavcodec.so.62")
    assert link.issym() and link.linkname == "libavcodec.so.62.1.100"
    lock.lock_pack(
        requirements, lspec, pack, _probe_outputs(tmp_path, lspec), tmp_path / "lock.json"
    )


def test_requirements_cells_name_licence_source_and_probe_tokens() -> None:
    requirements = lock.load_requirements()
    cells = lock.runtime_cells(requirements)
    # Cross-file: the requirements describe exactly the cells the app knows.
    assert cells == [
        *((ai_models.WHISPER_CPP_RUNTIME, cell) for cell in ai_models.WHISPER_CPP_CELLS),
        *((ai_models.FFMPEG_RUNTIME, cell) for cell in ai_models.FFMPEG_CELLS),
    ]
    assert urlsplit(requirements["downloadsBase"]).hostname in ai_models.MEDIA_PACK_DOWNLOAD_HOSTS

    for runtime, cell in cells:
        spec = lock.cell_spec(requirements, runtime, cell)
        where = f"{runtime}/{cell}"
        assert spec["license"].strip(), where
        assert urlsplit(spec["source"]).scheme == "https", where
        assert spec["archive"] in ai_models.MEDIA_PACK_ARCHIVES, where
        assert spec["probes"] and all(
            p.get("contains") or p.get("listed") for p in spec["probes"]
        ), where
        assert lock.pin_problems(spec) == [], where
        tokens = {t for p in spec["probes"] for t in p.get("contains", []) + p.get("listed", [])}
        if runtime == ai_models.FFMPEG_RUNTIME:
            listed = {p["name"]: set(p["listed"]) for p in spec["probes"] if "listed" in p}
            assert set(ai_models.FFMPEG_REQUIRED_ENCODERS) <= listed["encoders"], where
            assert set(ai_models.FFMPEG_REQUIRED_FILTERS) <= listed["filters"], where
            assert any("--enable-nonfree" in p.get("absent", []) for p in spec["probes"]), where
            exe = ".exe" if spec["os"] == "windows" else ""
            assert spec["binaries"] == [f"bin/ffmpeg{exe}", f"bin/ffprobe{exe}"], where
            assets = {a["name"] for a in spec["upstream"]["assets"]}
            assert {rule["asset"] for rule in spec["stage"]} <= assets, where
            assert any(rule["dest"] == "LICENSE" for rule in spec["stage"]), where
        else:
            # The ASR invocation relies on these flags (spec FR-3.1).
            assert {"--vad", "--vad-model", "--output-json-full", "--max-context"} <= tokens
            assert spec["version"] == spec["upstream"]["tag"] == ai_models.WHISPER_CPP_RELEASE_TAG
            flags = set(spec["cmakeFlags"])
            assert "-DBUILD_SHARED_LIBS=OFF" in flags and "-DGGML_OPENMP=OFF" in flags, where
            metal = cell.endswith("-metal")
            assert ("-DGGML_METAL=ON" in flags) is metal, where
            assert ("-DGGML_METAL_EMBED_LIBRARY=ON" in flags) is metal, where

    # Every cell plans onto its own runner; unknown selections are refused.
    planned = lock.plan(requirements, "all")
    assert sorted((e["runtime"], e["cell"]) for rows in planned.values() for e in rows) == sorted(
        cells
    )
    assert lock.plan(requirements, "ffmpeg/linux-arm64")["whisper_cpp"] == []
    with pytest.raises(lock.LockError, match="unknown"):
        lock.plan(requirements, "ffmpeg/windows-arm64")

    # The shipped manifest agrees with the requirements and publishes nothing
    # the committed lock does not cover.
    shipped = json.loads(ai_models.MEDIA_RUNTIME_MANIFEST_PATH.read_text(encoding="utf-8"))
    parsed = ai_models.parse_media_runtime_manifest(shipped)
    locked = (
        json.loads(lock.DEFAULT_LOCK.read_text(encoding="utf-8"))
        if lock.DEFAULT_LOCK.is_file()
        else {}
    )
    for runtime, cell_id in cells:
        spec = lock.cell_spec(requirements, runtime, cell_id)
        cell = parsed.cell(runtime, cell_id)
        assert cell is not None, f"{runtime}/{cell_id}"
        assert (cell.license, cell.source) == (spec["license"], spec["source"])
        if cell.published:
            entry = locked.get("packs", {}).get(f"{runtime}/{cell_id}")
            assert entry is not None and entry["sha256"] == cell.sha256
            assert (cell.archive, cell.version) == (spec["archive"], spec["version"])


def _job_runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


def test_workflow_is_opt_in_plan_shrunk_and_never_gates_on_matrix() -> None:
    # GitHub rejects a whole workflow file whose job-level ``if:`` names
    # ``matrix`` — guard every workflow in the repo, not only this one.
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in doc.get("jobs", {}).items():
            assert "matrix" not in str(job.get("if", "")), f"{path.name}:{name}"

    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads ``on`` as True
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"cells", "publish", "downloads_base"}
    assert inputs["publish"]["type"] == "boolean" and inputs["publish"]["default"] is False
    requirements = lock.load_requirements()
    assert inputs["downloads_base"]["default"] == requirements["downloadsBase"]
    assert workflow["permissions"] == {"contents": "read"}

    jobs = workflow["jobs"]
    # The plan job's outputs are exactly what the plan command emits.
    emitted = {line.split("=", 1)[0] for line in lock.plan_outputs(lock.plan(requirements, "all"))}
    assert set(jobs["plan"]["outputs"]) == emitted
    assert "lock_media_runtime.py plan" in _job_runs(jobs["plan"])
    for name in ("whisper", "ffmpeg"):
        job = jobs[name]
        assert job["needs"] == "plan"
        assert str(job["strategy"]["matrix"]).startswith("${{ fromJSON(needs.plan.outputs.")
        assert "needs.plan.outputs." in job["if"]
        runs = _job_runs(job)
        for command in ("smoke", "pack", "lock"):
            assert f"lock_media_runtime.py {command}" in runs, f"{name}: {command}"
    whisper_runs = _job_runs(jobs["whisper"])
    assert "codesign --force --sign -" in whisper_runs
    assert "codesign --verify" in whisper_runs
    assert "otool -L" in whisper_runs
    ffmpeg_runs = _job_runs(jobs["ffmpeg"])
    assert "lock_media_runtime.py fetch" in ffmpeg_runs
    assert "lock_media_runtime.py stage" in ffmpeg_runs

    publish = jobs["publish"]
    assert "inputs.publish" in publish["if"]
    assert publish["permissions"] == {"contents": "write"}
    publish_runs = _job_runs(publish)
    assert "--publish-root pages-worktree/packs" in publish_runs
    assert "render_media_runtime_manifests.py" in publish_runs
    assert "git push origin HEAD:gh-pages" in publish_runs
    assert publish_runs.index("HEAD:gh-pages") < publish_runs.index("HEAD:main")
    assert "src/vienetts_app/core/media_runtime_manifests.json" in publish_runs
    assert "packaging/media-runtime-lock.json" in publish_runs
