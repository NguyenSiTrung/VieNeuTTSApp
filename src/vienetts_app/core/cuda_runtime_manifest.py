"""Pinned, checksum-verified CUDA runtime wheel manifests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class RuntimeWheel:
    filename: str
    url: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CudaRuntimeManifest:
    format_version: str
    platform_key: str
    python_tag: str
    wheels: tuple[RuntimeWheel, ...]


_WHEEL_URLS = {
    ("certifi-2026.7.22-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/0b/a7/71ac2cff56fec219ed242bb11b8efb69fcc4bec75db06fb7bfe35de520e6/"
        "certifi-2026.7.22-py3-none-any.whl"
    ),
    (
        "charset_normalizer-3.5.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ): (
        "https://files.pythonhosted.org/packages/fb/af/63240b0c0248c075c2535a1f1bd992821d8251b9f173abc13329661d09e4/"
        "charset_normalizer-3.5.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ),
    ("charset_normalizer-3.5.1-cp313-cp313-win_amd64.whl"): (
        "https://files.pythonhosted.org/packages/8a/33/56d97ade41c8db611e727168c52ae46c9224c362ec28d4b65d7e9869e8da/"
        "charset_normalizer-3.5.1-cp313-cp313-win_amd64.whl"
    ),
    ("colorama-0.4.6-py2.py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/d1/d6/3965ed04c63042e047cb6a3e6ed1a63a35087b6a609aa3a15ed8ac56c221/"
        "colorama-0.4.6-py2.py3-none-any.whl"
    ),
    ("filelock-3.32.5-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/36/d2/b70a31e13d04456d28493f31d2aa087e99eeb2767ef0293b2625727ccb8c/"
        "filelock-3.32.5-py3-none-any.whl"
    ),
    ("fsspec-2026.7.0-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/fd/3c/6a2bf344106328fd04963664a60b9bb6496fc25df8e962fcdc1367285fb9/"
        "fsspec-2026.7.0-py3-none-any.whl"
    ),
    ("hf_xet-1.6.0-cp38-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/67/4e/a28359bf1c1ecf11eba22123168c138698f7cb576ac678f5a2e16cd5da08/"
        "hf_xet-1.6.0-cp38-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("huggingface_hub-0.36.2-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/a8/af/48ac8483240de756d2438c380746e7130d1c6f75802ef22f3c6d49982787/"
        "huggingface_hub-0.36.2-py3-none-any.whl"
    ),
    ("idna-3.19-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/57/b0/0e52c878c53f245edd3a11020f20979b3f490f245af532c7cae3027754b5/"
        "idna-3.19-py3-none-any.whl"
    ),
    ("jinja2-3.1.6-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/62/a1/3d680cbfd5f4b8f15abc1d571870c5fc3e594bb582bc3b64ea099db13e56/"
        "jinja2-3.1.6-py3-none-any.whl"
    ),
    (
        "markupsafe-3.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ): (
        "https://files.pythonhosted.org/packages/a9/21/9b05698b46f218fc0e118e1f8168395c65c8a2c750ae2bab54fc4bd4e0e8/"
        "markupsafe-3.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ),
    ("markupsafe-3.0.3-cp313-cp313-win_amd64.whl"): (
        "https://files.pythonhosted.org/packages/05/73/c4abe620b841b6b791f2edc248f556900667a5a1cf023a6646967ae98335/"
        "markupsafe-3.0.3-cp313-cp313-win_amd64.whl"
    ),
    ("mpmath-1.3.0-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/43/e3/7d92a15f894aa0c9c4b49b8ee9ac9850d6e63b03c9c32c0367a13ae62209/"
        "mpmath-1.3.0-py3-none-any.whl"
    ),
    ("networkx-3.6.1-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/9e/c9/b2622292ea83fbb4ec318f5b9ab867d0a28ab43c5717bb85b0a5f6b3b0a4/"
        "networkx-3.6.1-py3-none-any.whl"
    ),
    ("numpy-2.5.2-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/7b/44/59a1eb68e773c4098d107ef34a0dbdeca501d72ffcfbff9a7707343921ce/"
        "numpy-2.5.2-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
    ),
    ("numpy-2.5.2-cp313-cp313-win_amd64.whl"): (
        "https://files.pythonhosted.org/packages/15/20/f3489f86d81ea460b2bcdceaed094142ca6579f6be0ec527b781d39afe68/"
        "numpy-2.5.2-cp313-cp313-win_amd64.whl"
    ),
    ("nvidia_cublas_cu12-12.8.4.1-py3-none-manylinux_2_27_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/dc/61/e24b560ab2e2eaeb3c839129175fb330dfcfc29e5203196e5541a4c44682/"
        "nvidia_cublas_cu12-12.8.4.1-py3-none-manylinux_2_27_x86_64.whl"
    ),
    ("nvidia_cuda_cupti_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/f8/02/2adcaa145158bf1a8295d83591d22e4103dbfd821bcaf6f3f53151ca4ffa/"
        "nvidia_cuda_cupti_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("nvidia_cuda_nvrtc_cu12-12.8.93-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/05/6b/32f747947df2da6994e999492ab306a903659555dddc0fbdeb9d71f75e52/"
        "nvidia_cuda_nvrtc_cu12-12.8.93-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl"
    ),
    ("nvidia_cuda_runtime_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/0d/9b/a997b638fcd068ad6e4d53b8551a7d30fe8b404d6f1804abf1df69838932/"
        "nvidia_cuda_runtime_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("nvidia_cudnn_cu12-9.10.2.21-py3-none-manylinux_2_27_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/ba/51/e123d997aa098c61d029f76663dedbfb9bc8dcf8c60cbd6adbe42f76d049/"
        "nvidia_cudnn_cu12-9.10.2.21-py3-none-manylinux_2_27_x86_64.whl"
    ),
    ("nvidia_cufft_cu12-11.3.3.83-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/1f/13/ee4e00f30e676b66ae65b4f08cb5bcbb8392c03f54f2d5413ea99a5d1c80/"
        "nvidia_cufft_cu12-11.3.3.83-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("nvidia_cufile_cu12-1.13.1.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/bb/fe/1bcba1dfbfb8d01be8d93f07bfc502c93fa23afa6fd5ab3fc7c1df71038a/"
        "nvidia_cufile_cu12-1.13.1.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("nvidia_curand_cu12-10.3.9.90-py3-none-manylinux_2_27_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/fb/aa/6584b56dc84ebe9cf93226a5cde4d99080c8e90ab40f0c27bda7a0f29aa1/"
        "nvidia_curand_cu12-10.3.9.90-py3-none-manylinux_2_27_x86_64.whl"
    ),
    ("nvidia_cusolver_cu12-11.7.3.90-py3-none-manylinux_2_27_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/85/48/9a13d2975803e8cf2777d5ed57b87a0b6ca2cc795f9a4f59796a910bfb80/"
        "nvidia_cusolver_cu12-11.7.3.90-py3-none-manylinux_2_27_x86_64.whl"
    ),
    ("nvidia_cusparse_cu12-12.5.8.93-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/c2/f5/e1854cb2f2bcd4280c44736c93550cc300ff4b8c95ebe370d0aa7d2b473d/"
        "nvidia_cusparse_cu12-12.5.8.93-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("nvidia_cusparselt_cu12-0.7.1-py3-none-manylinux2014_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/56/79/12978b96bd44274fe38b5dde5cfb660b1d114f70a65ef962bcbbed99b549/"
        "nvidia_cusparselt_cu12-0.7.1-py3-none-manylinux2014_x86_64.whl"
    ),
    ("nvidia_nccl_cu12-2.27.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/5c/5b/4e4fff7bad39adf89f735f2bc87248c81db71205b62bcc0d5ca5b606b3c3/"
        "nvidia_nccl_cu12-2.27.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("nvidia_nvjitlink_cu12-12.8.93-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/f6/74/86a07f1d0f42998ca31312f998bd3b9a7eff7f52378f4f270c8679c77fb9/"
        "nvidia_nvjitlink_cu12-12.8.93-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl"
    ),
    ("nvidia_nvtx_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/a2/eb/86626c1bbc2edb86323022371c39aa48df6fd8b0a1647bc274577f72e90b/"
        "nvidia_nvtx_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    ),
    ("packaging-26.3-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/63/34/ba1c580383c9eada3711951fef0795c80b829a078d72188184bcab9dd527/"
        "packaging-26.3-py3-none-any.whl"
    ),
    (
        "pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ): (
        "https://files.pythonhosted.org/packages/74/27/e5b8f34d02d9995b80abcef563ea1f8b56d20134d8f4e5e81733b1feceb2/"
        "pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ),
    ("pyyaml-6.0.3-cp313-cp313-win_amd64.whl"): (
        "https://files.pythonhosted.org/packages/97/c9/39d5b874e8b28845e4ec2202b5da735d0199dbe5b8fb85f91398814a9a46/"
        "pyyaml-6.0.3-cp313-cp313-win_amd64.whl"
    ),
    (
        "regex-2026.9.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ): (
        "https://files.pythonhosted.org/packages/3f/a6/e9a59b507cdf7a9735df4bab92b4ea9d2ca2e665c6de14c112db7e3c0926/"
        "regex-2026.9.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    ),
    ("regex-2026.9.3-cp313-cp313-win_amd64.whl"): (
        "https://files.pythonhosted.org/packages/6d/25/6d20a309c2e4b554cc33579dd55b0bb50d0c2ace7ce6f084be2327c330fc/"
        "regex-2026.9.3-cp313-cp313-win_amd64.whl"
    ),
    ("requests-2.34.2-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/a0/f4/c67b0b3f1b9245e8d266f0f112c500d50e5b4e83cb6f3b71b6528104182a/"
        "requests-2.34.2-py3-none-any.whl"
    ),
    ("safetensors-0.8.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/28/50/f203ff3a3ddfe19308efc83c5a3a29ed02bf786732ec35e68bf9162f3365/"
        "safetensors-0.8.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    ),
    ("safetensors-0.8.0-cp310-abi3-win_amd64.whl"): (
        "https://files.pythonhosted.org/packages/1b/6d/3fba214c1e5e0f69991677ec3bc17023f0421776975e1de0c682dca475e2/"
        "safetensors-0.8.0-cp310-abi3-win_amd64.whl"
    ),
    ("setuptools-84.0.0-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/"
        "setuptools-84.0.0-py3-none-any.whl"
    ),
    ("sympy-1.14.0-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/a2/09/77d55d46fd61b4a135c444fc97158ef34a095e5681d0a6c10b75bf356191/"
        "sympy-1.14.0-py3-none-any.whl"
    ),
    ("tokenizers-0.22.2-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/2e/76/932be4b50ef6ccedf9d3c6639b056a967a86258c6d9200643f01269211ca/"
        "tokenizers-0.22.2-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    ),
    ("tokenizers-0.22.2-cp39-abi3-win_amd64.whl"): (
        "https://files.pythonhosted.org/packages/65/71/0670843133a43d43070abeb1949abfdef12a86d490bea9cd9e18e37c5ff7/"
        "tokenizers-0.22.2-cp39-abi3-win_amd64.whl"
    ),
    ("torch-2.8.0+cu128-cp313-cp313-manylinux_2_28_x86_64.whl"): (
        "https://download.pytorch.org/whl/cu128/"
        "torch-2.8.0+cu128-cp313-cp313-manylinux_2_28_x86_64.whl"
    ),
    ("torch-2.8.0+cu128-cp313-cp313-win_amd64.whl"): (
        "https://download.pytorch.org/whl/cu128/torch-2.8.0+cu128-cp313-cp313-win_amd64.whl"
    ),
    ("torchaudio-2.8.0+cu128-cp313-cp313-manylinux_2_28_x86_64.whl"): (
        "https://download.pytorch.org/whl/cu128/"
        "torchaudio-2.8.0+cu128-cp313-cp313-manylinux_2_28_x86_64.whl"
    ),
    ("torchaudio-2.8.0+cu128-cp313-cp313-win_amd64.whl"): (
        "https://download.pytorch.org/whl/cu128/torchaudio-2.8.0+cu128-cp313-cp313-win_amd64.whl"
    ),
    ("tqdm-4.70.0-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/f9/1c/01bfd571a64e7f270e6bab5e33777debe0edc56759233ce84f27dec92d14/"
        "tqdm-4.70.0-py3-none-any.whl"
    ),
    ("transformers-4.57.6-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/03/b8/e484ef633af3887baeeb4b6ad12743363af7cce68ae51e938e00aaa0529d/"
        "transformers-4.57.6-py3-none-any.whl"
    ),
    ("triton-3.4.0-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"): (
        "https://files.pythonhosted.org/packages/30/7b/0a685684ed5322d2af0bddefed7906674f67974aa88b0fae6e82e3b766f6/"
        "triton-3.4.0-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
    ),
    ("typing_extensions-4.16.0-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/"
        "typing_extensions-4.16.0-py3-none-any.whl"
    ),
    ("urllib3-2.7.0-py3-none-any.whl"): (
        "https://files.pythonhosted.org/packages/7f/3e/5db95bcf282c52709639744ca2a8b149baccf648e39c8cc87553df9eae0c/"
        "urllib3-2.7.0-py3-none-any.whl"
    ),
}


def _w(filename: str, size_bytes: int, sha256: str) -> RuntimeWheel:
    return RuntimeWheel(filename, _WHEEL_URLS[filename], size_bytes, sha256)


_MANIFESTS = {
    "windows-x64": CudaRuntimeManifest(
        format_version="cuda-cu128-v1",
        platform_key="windows-x64",
        python_tag="cp313",
        wheels=(
            _w(
                "certifi-2026.7.22-py3-none-any.whl",
                136983,
                "62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775",
            ),
            _w(
                "charset_normalizer-3.5.1-cp313-cp313-win_amd64.whl",
                199295,
                "aea996a6aba25260827c9ea511d1addfde2da9eb686ac961838509086188b7e6",
            ),
            _w(
                "colorama-0.4.6-py2.py3-none-any.whl",
                25335,
                "4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6",
            ),
            _w(
                "filelock-3.32.5-py3-none-any.whl",
                100003,
                "142cd9fa77a872c5e78c62329a0d15278fadc686eb89e760017968961a4fd6b2",
            ),
            _w(
                "fsspec-2026.7.0-py3-none-any.whl",
                206583,
                "b57ddbafedfaef7018c1ecab32aa200a9d7ca26b77965f64e48b70061249d279",
            ),
            _w(
                "huggingface_hub-0.36.2-py3-none-any.whl",
                566395,
                "48f0c8eac16145dfce371e9d2d7772854a4f591bcb56c9cf548accf531d54270",
            ),
            _w(
                "idna-3.19-py3-none-any.whl",
                68550,
                "815e7be7a7806d54abb586dc943addc79e8b2ee16915059658cbeff4b1b43bf4",
            ),
            _w(
                "jinja2-3.1.6-py3-none-any.whl",
                134899,
                "85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67",
            ),
            _w(
                "markupsafe-3.0.3-cp313-cp313-win_amd64.whl",
                15113,
                "9a1abfdc021a164803f4d485104931fb8f8c1efd55bc6b748d2f5774e78b62c5",
            ),
            _w(
                "mpmath-1.3.0-py3-none-any.whl",
                536198,
                "a0b2b9fe80bbcd81a6647ff13108738cfb482d481d826cc0e02f5b35e5c88d2c",
            ),
            _w(
                "networkx-3.6.1-py3-none-any.whl",
                2068504,
                "d47fbf302e7d9cbbb9e2555a0d267983d2aa476bac30e90dfbe5669bd57f3762",
            ),
            _w(
                "numpy-2.5.2-cp313-cp313-win_amd64.whl",
                12460532,
                "85aaccb24182c25df891ad0ec333585967e115269d5f1b17f2c9ae005bc96657",
            ),
            _w(
                "packaging-26.3-py3-none-any.whl",
                129956,
                "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
            ),
            _w(
                "pyyaml-6.0.3-cp313-cp313-win_amd64.whl",
                154090,
                "79005a0d97d5ddabfeeea4cf676af11e647e41d81c9a7722a193022accdb6b7c",
            ),
            _w(
                "regex-2026.9.3-cp313-cp313-win_amd64.whl",
                277741,
                "185c1ae881856208dda05708b6c908aff76878e59c998c8548d365c1bbcaf1bd",
            ),
            _w(
                "requests-2.34.2-py3-none-any.whl",
                73075,
                "2a0d60c172f83ac6ab31e4554906c0f3b3588d37b5cb939b1c061f4907e278e0",
            ),
            _w(
                "safetensors-0.8.0-cp310-abi3-win_amd64.whl",
                355540,
                "096ec1a98435df7beb08853bb5aa9081a84f23d0adc67ed1a0a10550f608373f",
            ),
            _w(
                "setuptools-84.0.0-py3-none-any.whl",
                818216,
                "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670",
            ),
            _w(
                "sympy-1.14.0-py3-none-any.whl",
                6299353,
                "e091cc3e99d2141a0ba2847328f5479b05d94a6635cb96148ccb3f34671bd8f5",
            ),
            _w(
                "tokenizers-0.22.2-cp39-abi3-win_amd64.whl",
                2747786,
                "c9ea31edff2968b44a88f97d784c2f16dc0729b8b143ed004699ebca91f05c48",
            ),
            _w(
                "torch-2.8.0+cu128-cp313-cp313-win_amd64.whl",
                3461390892,
                "9e20646802b7fc295c1f8b45fefcfc9fb2e4ec9cbe8593443cd2b9cc307c8405",
            ),
            _w(
                "torchaudio-2.8.0+cu128-cp313-cp313-win_amd64.whl",
                4673227,
                "3146bbd48992d215f6bb1aef9626d734c3180b377791ded2a4d4d2c0e63c0cc2",
            ),
            _w(
                "tqdm-4.70.0-py3-none-any.whl",
                80184,
                "7f585706bfddbdebf89daac705b2dfcc16890130727d3197ca62c732b4310953",
            ),
            _w(
                "transformers-4.57.6-py3-none-any.whl",
                11993498,
                "4c9e9de11333ddfe5114bc872c9f370509198acf0b87a832a0ab9458e2bd0550",
            ),
            _w(
                "typing_extensions-4.16.0-py3-none-any.whl",
                45571,
                "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
            ),
            _w(
                "urllib3-2.7.0-py3-none-any.whl",
                131087,
                "9fb4c81ebbb1ce9531cce37674bbc6f1360472bc18ca9a553ede278ef7276897",
            ),
        ),
    ),
    "linux-x64": CudaRuntimeManifest(
        format_version="cuda-cu128-v1",
        platform_key="linux-x64",
        python_tag="cp313",
        wheels=(
            _w(
                "certifi-2026.7.22-py3-none-any.whl",
                136983,
                "62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775",
            ),
            _w(
                "charset_normalizer-3.5.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
                250638,
                "62b55f6722735a6c472f88361cde6640608773d9443cebdbb51abf436a1fcdd3",
            ),
            _w(
                "filelock-3.32.5-py3-none-any.whl",
                100003,
                "142cd9fa77a872c5e78c62329a0d15278fadc686eb89e760017968961a4fd6b2",
            ),
            _w(
                "fsspec-2026.7.0-py3-none-any.whl",
                206583,
                "b57ddbafedfaef7018c1ecab32aa200a9d7ca26b77965f64e48b70061249d279",
            ),
            _w(
                "hf_xet-1.6.0-cp38-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                4464663,
                "d62671bb130879cef0ee4c9ebe47a14af6c66ec53e6d84dc15936e5ffdfac82f",
            ),
            _w(
                "huggingface_hub-0.36.2-py3-none-any.whl",
                566395,
                "48f0c8eac16145dfce371e9d2d7772854a4f591bcb56c9cf548accf531d54270",
            ),
            _w(
                "idna-3.19-py3-none-any.whl",
                68550,
                "815e7be7a7806d54abb586dc943addc79e8b2ee16915059658cbeff4b1b43bf4",
            ),
            _w(
                "jinja2-3.1.6-py3-none-any.whl",
                134899,
                "85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67",
            ),
            _w(
                "markupsafe-3.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
                22980,
                "ccfcd093f13f0f0b7fdd0f198b90053bf7b2f02a3927a30e63f3ccc9df56b676",
            ),
            _w(
                "mpmath-1.3.0-py3-none-any.whl",
                536198,
                "a0b2b9fe80bbcd81a6647ff13108738cfb482d481d826cc0e02f5b35e5c88d2c",
            ),
            _w(
                "networkx-3.6.1-py3-none-any.whl",
                2068504,
                "d47fbf302e7d9cbbb9e2555a0d267983d2aa476bac30e90dfbe5669bd57f3762",
            ),
            _w(
                "numpy-2.5.2-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                16709995,
                "29b86ff8a6cc556b47ec6b64b194815cc80e6bf5eedcc6cddfd65318cb0b4eee",
            ),
            _w(
                "nvidia_cublas_cu12-12.8.4.1-py3-none-manylinux_2_27_x86_64.whl",
                594346921,
                "8ac4e771d5a348c551b2a426eda6193c19aa630236b418086020df5ba9667142",
            ),
            _w(
                "nvidia_cuda_cupti_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                10248621,
                "ea0cb07ebda26bb9b29ba82cda34849e73c166c18162d3913575b0c9db9a6182",
            ),
            _w(
                "nvidia_cuda_nvrtc_cu12-12.8.93-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl",
                88040029,
                "a7756528852ef889772a84c6cd89d41dfa74667e24cca16bb31f8f061e3e9994",
            ),
            _w(
                "nvidia_cuda_runtime_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                954765,
                "adade8dcbd0edf427b7204d480d6066d33902cab2a4707dcfc48a2d0fd44ab90",
            ),
            _w(
                "nvidia_cudnn_cu12-9.10.2.21-py3-none-manylinux_2_27_x86_64.whl",
                706758467,
                "949452be657fa16687d0930933f032835951ef0892b37d2d53824d1a84dc97a8",
            ),
            _w(
                "nvidia_cufft_cu12-11.3.3.83-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                193118695,
                "4d2dd21ec0b88cf61b62e6b43564355e5222e4a3fb394cac0db101f2dd0d4f74",
            ),
            _w(
                "nvidia_cufile_cu12-1.13.1.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                1197834,
                "1d069003be650e131b21c932ec3d8969c1715379251f8d23a1860554b1cb24fc",
            ),
            _w(
                "nvidia_curand_cu12-10.3.9.90-py3-none-manylinux_2_27_x86_64.whl",
                63619976,
                "b32331d4f4df5d6eefa0554c565b626c7216f87a06a4f56fab27c3b68a830ec9",
            ),
            _w(
                "nvidia_cusolver_cu12-11.7.3.90-py3-none-manylinux_2_27_x86_64.whl",
                267506905,
                "4376c11ad263152bd50ea295c05370360776f8c3427b30991df774f9fb26c450",
            ),
            _w(
                "nvidia_cusparse_cu12-12.5.8.93-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                288216466,
                "1ec05d76bbbd8b61b06a80e1eaf8cf4959c3d4ce8e711b65ebd0443bb0ebb13b",
            ),
            _w(
                "nvidia_cusparselt_cu12-0.7.1-py3-none-manylinux2014_x86_64.whl",
                287193691,
                "f1bb701d6b930d5a7cea44c19ceb973311500847f81b634d802b7b539dc55623",
            ),
            _w(
                "nvidia_nccl_cu12-2.27.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                322364134,
                "adf27ccf4238253e0b826bce3ff5fa532d65fc42322c8bfdfaf28024c0fbe039",
            ),
            _w(
                "nvidia_nvjitlink_cu12-12.8.93-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl",
                39254836,
                "81ff63371a7ebd6e6451970684f916be2eab07321b73c9d244dc2b4da7f73b88",
            ),
            _w(
                "nvidia_nvtx_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                89954,
                "5b17e2001cc0d751a5bc2c6ec6d26ad95913324a4adb86788c944f8ce9ba441f",
            ),
            _w(
                "packaging-26.3-py3-none-any.whl",
                129956,
                "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
            ),
            _w(
                "pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
                801626,
                "0f29edc409a6392443abf94b9cf89ce99889a1dd5376d94316ae5145dfedd5d6",
            ),
            _w(
                "regex-2026.9.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
                801945,
                "27f0809798071f56fb1bc536bb93714a95e8ed2ec0dfd869f095deebb30fd11a",
            ),
            _w(
                "requests-2.34.2-py3-none-any.whl",
                73075,
                "2a0d60c172f83ac6ab31e4554906c0f3b3588d37b5cb939b1c061f4907e278e0",
            ),
            _w(
                "safetensors-0.8.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                516040,
                "fd6f3f93c9a0a7cc2788ee63fb763353d4bd2e89b0751bc78fcf7dda00bea774",
            ),
            _w(
                "setuptools-84.0.0-py3-none-any.whl",
                818216,
                "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670",
            ),
            _w(
                "sympy-1.14.0-py3-none-any.whl",
                6299353,
                "e091cc3e99d2141a0ba2847328f5479b05d94a6635cb96148ccb3f34671bd8f5",
            ),
            _w(
                "tokenizers-0.22.2-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                3274982,
                "369cc9fc8cc10cb24143873a0d95438bb8ee257bb80c71989e3ee290e8d72c67",
            ),
            _w(
                "torch-2.8.0+cu128-cp313-cp313-manylinux_2_28_x86_64.whl",
                889052836,
                "3a852369a38dec343d45ecd0bc3660f79b88a23e0c878d18707f7c13bf49538f",
            ),
            _w(
                "torchaudio-2.8.0+cu128-cp313-cp313-manylinux_2_28_x86_64.whl",
                3940953,
                "410bb8ea46225efe658e5d27a3802c181a2255913003621a5d25a51aca8018d9",
            ),
            _w(
                "tqdm-4.70.0-py3-none-any.whl",
                80184,
                "7f585706bfddbdebf89daac705b2dfcc16890130727d3197ca62c732b4310953",
            ),
            _w(
                "transformers-4.57.6-py3-none-any.whl",
                11993498,
                "4c9e9de11333ddfe5114bc872c9f370509198acf0b87a832a0ab9458e2bd0550",
            ),
            _w(
                "triton-3.4.0-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                155569223,
                "00be2964616f4c619193cb0d1b29a99bd4b001d7dc333816073f92cf2a8ccdeb",
            ),
            _w(
                "typing_extensions-4.16.0-py3-none-any.whl",
                45571,
                "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
            ),
            _w(
                "urllib3-2.7.0-py3-none-any.whl",
                131087,
                "9fb4c81ebbb1ce9531cce37674bbc6f1360472bc18ca9a553ede278ef7276897",
            ),
        ),
    ),
}


def _validate_manifests(manifests: dict[str, CudaRuntimeManifest]) -> None:
    for platform_key, manifest in manifests.items():
        if manifest.platform_key != platform_key:
            raise ValueError(f"manifest platform key mismatch: {platform_key!r}")
        filenames: set[str] = set()
        for wheel in manifest.wheels:
            if wheel.filename in filenames:
                raise ValueError(f"duplicate wheel filename: {wheel.filename}")
            filenames.add(wheel.filename)
            parsed = urlsplit(wheel.url)
            if parsed.scheme != "https":
                raise ValueError(f"wheel URL must use HTTPS: {wheel.url}")
            if parsed.hostname not in {"download.pytorch.org", "files.pythonhosted.org"}:
                raise ValueError(f"wheel URL host is unsupported: {wheel.url}")
            if parsed.query or parsed.fragment:
                raise ValueError(f"wheel URL must be a direct wheel artifact: {wheel.url}")
            if PurePosixPath(
                unquote(parsed.path)
            ).name != wheel.filename or not parsed.path.endswith(".whl"):
                raise ValueError(f"wheel URL must be a direct wheel artifact: {wheel.url}")
            if parsed.hostname == "download.pytorch.org" and not parsed.path.startswith(
                "/whl/cu128/"
            ):
                raise ValueError(f"wheel URL must be a CUDA 12.8 artifact: {wheel.url}")
            if parsed.hostname == "files.pythonhosted.org" and not parsed.path.startswith(
                "/packages/"
            ):
                raise ValueError(f"wheel URL must be a PyPI wheel artifact: {wheel.url}")
            if not re.fullmatch(r"[0-9a-f]{64}", wheel.sha256):
                raise ValueError(f"wheel SHA-256 is invalid: {wheel.filename}")
            if (
                not isinstance(wheel.size_bytes, int)
                or isinstance(wheel.size_bytes, bool)
                or wheel.size_bytes <= 0
            ):
                raise ValueError(f"wheel size is invalid: {wheel.filename}")


_validate_manifests(_MANIFESTS)


def manifest_for_platform(platform_key: str) -> CudaRuntimeManifest | None:
    """Return the verified CUDA runtime manifest for a supported platform."""
    return _MANIFESTS.get(platform_key)
