"""fetch_models CLI: the --backbone override flows into downloads + manifest.

The offline bundle must match whatever repo the app is configured to use,
so the script records the repo it actually fetched in manifest["repos"].
"""

import json
from pathlib import Path

import scripts.fetch_models as fm


def _install_fake_downloads(monkeypatch) -> list:
    """Replace snapshot_download with a recorder that materializes one file."""
    calls: list = []

    def fake_download(repo: str, local_dir: str, allow_patterns: list[str], **kwargs) -> None:
        calls.append((repo, Path(local_dir), list(allow_patterns), dict(kwargs)))
        out = Path(local_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "artifact.onnx").write_bytes(b"x")

    monkeypatch.setattr(fm, "snapshot_download", fake_download)
    return calls


class TestBackboneOverride:
    def test_default_fetches_official_backbone(self, tmp_path, monkeypatch) -> None:
        calls = _install_fake_downloads(monkeypatch)

        assert fm.main(["--out", str(tmp_path)]) == 0

        assert [repo for repo, _, _, _ in calls] == [fm.BACKBONE_REPO, fm.CODEC_REPO]
        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["repos"] == {"backbone": fm.BACKBONE_REPO, "codec": fm.CODEC_REPO}

    def test_backbone_flag_changes_repo_and_manifest(self, tmp_path, monkeypatch) -> None:
        calls = _install_fake_downloads(monkeypatch)

        assert fm.main(["--out", str(tmp_path), "--backbone", "someone/vieneu-tts-custom"]) == 0

        assert calls[0][0] == "someone/vieneu-tts-custom"
        assert calls[1][0] == fm.CODEC_REPO  # codec repo is not user-configurable
        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["repos"]["backbone"] == "someone/vieneu-tts-custom"


class TestOfficialRevisionPinning:
    def test_official_fetch_pins_revisions(self, tmp_path, monkeypatch) -> None:
        from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST

        calls = _install_fake_downloads(monkeypatch)

        assert fm.main(["--out", str(tmp_path)]) == 0

        by_repo = {repo: kwargs for repo, _, _, kwargs in calls}
        assert (
            by_repo[fm.BACKBONE_REPO].get("revision") == OFFICIAL_MODEL_MANIFEST.backbone_revision
        )
        assert by_repo[fm.CODEC_REPO].get("revision") == OFFICIAL_MODEL_MANIFEST.codec_revision

    def test_official_manifest_writes_shared_format_metadata(self, tmp_path, monkeypatch) -> None:
        from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST

        _install_fake_downloads(monkeypatch)

        assert fm.main(["--out", str(tmp_path)]) == 0

        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["format"] == OFFICIAL_MODEL_MANIFEST.format_version
        assert manifest["backbone_revision"] == OFFICIAL_MODEL_MANIFEST.backbone_revision
        assert manifest["codec_revision"] == OFFICIAL_MODEL_MANIFEST.codec_revision

    def test_custom_backbone_marks_manifest_not_official(self, tmp_path, monkeypatch) -> None:
        from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST

        _install_fake_downloads(monkeypatch)

        assert fm.main(["--out", str(tmp_path), "--backbone", "someone/vieneu-tts-custom"]) == 0

        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["format"] != OFFICIAL_MODEL_MANIFEST.format_version
        assert manifest["repos"]["backbone"] == "someone/vieneu-tts-custom"
