from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST


def test_official_manifest_is_immutable_and_complete() -> None:
    manifest = OFFICIAL_MODEL_MANIFEST
    assert manifest.format_version == "official-v1"
    assert manifest.backbone_revision == "2da0efab622a1722125991736524f080b751ef5b"
    assert manifest.codec_revision == "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae"
    assert {item.relative_path for item in manifest.files_for("backbone")} == {
        "config.json",
        "denoiser.onnx",
        "speaker_encoder.onnx",
        "onnx_int8/config.json",
        "onnx_int8/tokenizer.json",
        "onnx_int8/vieneu_acoustic_cached.onnx",
        "onnx_int8/vieneu_backbone_shared.data",
        "onnx_int8/vieneu_prefill.onnx",
        "onnx_int8/vieneu_decode_step.onnx",
        "onnx_int8/vieneu_v3_heads.npz",
    }
    assert len(manifest.files_for("codec")) == 6
    assert manifest.total_bytes == 327_034_699
    assert manifest.total_bytes == sum(item.size_bytes for item in manifest.files)
    assert manifest.required_free_bytes > manifest.total_bytes
