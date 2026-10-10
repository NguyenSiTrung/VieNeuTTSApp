import re

from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST


def test_official_manifest_pins_immutable_revisions_and_verifiable_files() -> None:
    manifest = OFFICIAL_MODEL_MANIFEST
    for revision in (manifest.backbone_revision, manifest.codec_revision):
        assert re.fullmatch(r"[0-9a-f]{40}", revision), revision
    assert manifest.files_for("backbone")
    assert manifest.files_for("codec")
    paths = [(item.repo_key, item.relative_path) for item in manifest.files]
    assert len(paths) == len(set(paths))
    for item in manifest.files:
        assert item.size_bytes > 0, item.relative_path
        assert re.fullmatch(r"[0-9a-f]{64}", item.sha256), item.relative_path
