# Release Hardening and Upstream Experiments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship verifiably model-free application artifacts that smoke-test
against a pinned managed model install, document release signing limits
truthfully, and establish evidence gates for optional upstream optimizations.

**Architecture:** Release CI caches the exact official model install by
manifest hash outside the frozen bundle, invokes the packaged binary with an
explicit app-data directory and offline mode, then validates output and bundle
contents. A local release-verification script produces checksums and a
privacy-safe component inventory. Separate experiment runners observe SDK
session behavior and candidate offline ONNX/ORT artifact conversion without
changing production inference until explicit output/init/RSS/cross-platform
gates pass.

**Tech Stack:** GitHub Actions, PyInstaller, Python stdlib/hashlib/json,
SoundFile/Riff smoke verification, `huggingface_hub`, optional ONNX Runtime
experiment tools, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-performance-optimization-implementation-design.md`

**Dependencies:** Implement after
`2026-09-03-startup-resource-profiles-and-ux.md`. This plan consumes the
pinned Phase 1 manifest, managed-model location, artifact-first smoke path,
and benchmark schema. It does not change production synthesis decisions
unless a separately approved experiment gate passes.

## Global Constraints

- Frozen application artifacts remain model-free. Do not add model graph,
  codec, speaker encoder, checkpoint, or Hugging Face cache files to
  `packaging/vienetts-app.spec`.
- Release CI uses the same revision-pinned official manifest as the app. It
  never downloads from a mutable Hub branch head.
- Packaged smoke runs use an explicit temporary application data directory and
  a verified managed model install; it must work with Hub access disabled.
- The release smoke test does not rely on global `HF_HOME`, a developer
  home directory, or a preexisting Hugging Face cache.
- A failed managed-model validation, missing model, or output mismatch fails
  CI before artifact publishing.
- Do not print cache paths, account names, credential values, model tokens,
  source text other than the fixed public smoke phrase, cloned voice samples,
  or generated audio contents in logs.
- Generate SHA-256 checksums for distributable archives and publish only
  checksum values/file names in release evidence.
- Apple Developer ID signing, notarization, Windows Authenticode signing, and
  any signing credentials require human-provided secrets and explicit release
  authorization. Do not invent secret names/values, certificates, identities,
  or bypass security controls.
- A no-credential macOS build remains explicitly labeled ad-hoc signed and
  unnotarized; documentation must retain the current Gatekeeper guidance.
- Optional upstream experiments are opt-in scripts. They must not alter
  `pyproject.toml` pins, frozen package contents, user model installs, or
  normal `TTSEngine` code.
- A production upstream optimization requires all four evidence gates:
  output compatibility, initialization latency, RSS, and supported-platform
  verification. It also needs an explicit design revision and user approval.
- No cloud telemetry, source-content upload, dual model residence,
  CUDA microbatching, or inference subprocess enters this release scope.
- Before each commit, run the task’s focused tests. Before Phase completion,
  run:
  `./.venv/bin/ruff check .`,
  `./.venv/bin/ruff format --check .`, and
  `QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest`.

## File Map

### New production-support files

- `scripts/verify_frozen_bundle.py`
  - Verifies a frozen one-dir/app bundle contains no managed model payloads
    and emits a privacy-safe file inventory/checksum report.
- `scripts/release_manifest.py`
  - Generates release archive checksums and records build/model-manifest
    identifiers without reading user content.
- `docs/release/managed-model-smoke.md`
  - Exact local and CI smoke procedure, model-free bundle rule, and signing
    credential boundary.
- `docs/release/signing-and-notarization.md`
  - Human-run setup/approval checklist, intentionally without credentials.

### New experiment files

- `scripts/experiments/measure_sdk_sessions.py`
  - Measures observable Vieneu/ORT session initialization and required
    session use for stream/infer/denoise modes.
- `scripts/experiments/compare_onnx_artifacts.py`
  - Compares candidate local ONNX/ORT artifacts against the official
    reference for deterministic public test corpus output statistics and
    resource metrics.
- `scripts/experiments/evaluate_experiment.py`
  - Evaluates JSONL against explicit compatibility/init/RSS/platform gates.
- `tests/unit/test_verify_frozen_bundle.py`
- `tests/unit/test_release_manifest.py`
- `tests/unit/test_experiment_gates.py`

### Modified production files

- `src/vienetts_app/__main__.py`
  - Makes `--smoke` resolve an explicit managed data directory and supports
    local-only smoke mode without a global cache.
- `src/vienetts_app/core/model_manager.py`
  - Exposes a read-only `managed_location_from_data_dir` helper used by smoke
    and never triggers download.
- `scripts/fetch_models.py`
  - Supports a specified app-data destination and the shared pinned
    manifest/validation flow.
- `packaging/vienetts-app.spec`
  - Adds a tested invariant comment/list only; it continues to omit model
    data.
- `.github/workflows/release.yml`
  - Uses managed-model cache keyed by manifest, offline packaged smoke,
    bundle audit, checksums, and credential-gated signing/notarization
    verification.
- `README.md`
- `PROJECT_PLAN.md`
- `docs/performance/README.md`

### Modified tests

- `tests/unit/test_app_entry.py`
- `tests/unit/test_fetch_models.py`
- `tests/unit/test_model_manager.py`
- `tests/unit/test_package.py`
- `tests/unit/test_linux_packaging.py`
- `tests/smoke/test_e2e_flows.py`

---

### Task 1: Make the headless packaged smoke consume only managed local models

**Files:**

- Modify: `src/vienetts_app/__main__.py`
- Modify: `src/vienetts_app/core/model_manager.py`
- Modify: `scripts/fetch_models.py`
- Modify: `tests/unit/test_model_manager.py`
- Modify: `tests/unit/test_fetch_models.py`
- Modify: existing smoke CLI tests under `tests/unit/`

**Interfaces:**

- Produces CLI options:

```text
vienetts-app --smoke TEXT --data-dir PATH --offline -o PATH
python scripts/fetch_models.py --data-dir PATH
```

- Produces:

```python
def managed_location_from_data_dir(data_dir: Path) -> ManagedModelLocation | None: ...

def run_smoke(
    text: str,
    voice: str,
    output: str | Path,
    *,
    stream: bool = True,
    data_dir: Path | None = None,
    offline: bool = False,
    engine_factory: Callable[..., Any] | None = None,
    timeout: float = SMOKE_TIMEOUT_SECONDS,
) -> int: ...
```

- `--data-dir` is never serialized into logs, reports, or artifacts.
- `--offline` requires a managed location and fails before engine creation
  when it is not valid. It sets no global model-cache environment variable.

- [ ] **Step 1: Write failing local-smoke tests**

```python
def test_smoke_passes_verified_managed_paths_to_engine(tmp_path, monkeypatch) -> None:
    install_managed_fixture(tmp_path)
    observed: dict[str, object] = {}

    result = run_smoke(
        "Xin chào",
        "Adam",
        tmp_path / "out.wav",
        data_dir=tmp_path,
        offline=True,
        engine_factory=lambda **kwargs: observed.update(kwargs) or FakeEngine(),
    )

    assert result == 0
    assert observed["managed_model"].backbone_dir == tmp_path / "models/official-v1/backbone"


def test_offline_smoke_fails_before_engine_when_install_is_missing(tmp_path) -> None:
    created = False

    result = run_smoke(
        "Xin chào",
        "Adam",
        tmp_path / "out.wav",
        data_dir=tmp_path,
        offline=True,
        engine_factory=lambda **_kwargs: pytest.fail("engine must not be constructed"),
    )

    assert result == 1
    assert created is False
```

Complete the local-smoke matrix with these assertions:

- `scripts/fetch_models.py --data-dir <tmp>` uses the fake downloader and
  writes only the Phase 1 `models/official-v1` layout;
- parsing `--data-dir`, `--offline`, `--stream`, `--voice`, and `-o`
  round-trips their supplied values;
- the default smoke error/output contains no supplied `data_dir` string;
- a successful artifact-first smoke receives `SynthesisArtifact.path` and
  has no code path that accesses a raw `_audio` attribute.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:

```bash
./.venv/bin/pytest -n 0 \
  tests/unit/test_model_manager.py \
  tests/unit/test_fetch_models.py \
  tests/unit/test_app_entry.py -v
```

Expected: FAIL because `--data-dir`, `--offline`, and the local location
resolver do not exist.

- [ ] **Step 3: Implement local-only smoke setup**

Add parser arguments:

```python
parser.add_argument("--data-dir", type=Path, default=None)
parser.add_argument("--offline", action="store_true")
```

Resolve a location with `ModelManager(data_dir / "models").inspect()`, then
pass it into `TTSEngine(managed_model=location, backend="onnx")`. Do not
perform `install()` from the smoke command. When `offline` is true and
`status.location is None`, write the constant diagnostic
`"error: verified managed model is required for offline smoke"` to stderr and
return 1.

Migrate smoke completion to its Phase 3
`JobTerminal(value=SynthesisArtifact)` contract. Validate/copy the artifact
to `--output`; do not call `write_wav_file` against an in-memory array.
Use an injected `ManagedModelLocation` fixture in unit tests so no actual Hub
or model file download is required.

`fetch_models.py --data-dir` constructs only `ModelManager(data_dir / "models")`
and calls its pinned `install` method. Its existing user-facing source does
not accept arbitrary repository values for this option.

- [ ] **Step 4: Run focused local-smoke tests**

Run:

```bash
./.venv/bin/pytest -n 0 \
  tests/unit/test_model_manager.py \
  tests/unit/test_fetch_models.py \
  tests/unit/test_app_entry.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit local managed smoke**

```bash
git add src/vienetts_app/__main__.py src/vienetts_app/core/model_manager.py \
  scripts/fetch_models.py tests/unit/test_model_manager.py \
  tests/unit/test_fetch_models.py tests/unit/test_app_entry.py
git commit -m "test(release): smoke packaged app with managed models"
git notes add -m "Phase 6 Task 1: added explicit managed-data offline smoke contract."
```

### Task 2: Audit frozen bundle contents and create release checksums

**Files:**

- Create: `scripts/verify_frozen_bundle.py`
- Create: `scripts/release_manifest.py`
- Create: `tests/unit/test_verify_frozen_bundle.py`
- Create: `tests/unit/test_release_manifest.py`
- Modify: `packaging/vienetts-app.spec`
- Modify: `tests/unit/test_package.py`

**Interfaces:**

- Produces:

```python
FORBIDDEN_MODEL_NAMES = frozenset({
    "speaker_encoder.onnx",
    "denoiser.onnx",
    "vieneu_backbone_shared.data",
    "moss_audio_tokenizer_encoder.onnx",
    "moss_audio_tokenizer_decoder.onnx",
})

def verify_bundle(bundle_root: Path) -> dict[str, object]: ...
def release_entries(paths: Iterable[Path]) -> list[dict[str, str | int]]: ...
```

- CLI:

```text
python scripts/verify_frozen_bundle.py --bundle dist/VieNeuTTS
python scripts/release_manifest.py --input-dir pack --output pack/SHA256SUMS.json
```

- A bundle verification report contains only relative file names, file size,
  aggregate size, build manifest version, `model_files_found`, and a boolean
  `ok`. It does not include machine/user path prefixes.

- [ ] **Step 1: Write failing bundle/checksum tests**

```python
from scripts.verify_frozen_bundle import verify_bundle


def test_bundle_audit_rejects_managed_model_graph(tmp_path) -> None:
    bundle = tmp_path / "VieNeuTTS"
    graph = bundle / "vienetts_app" / "speaker_encoder.onnx"
    graph.parent.mkdir(parents=True)
    graph.write_bytes(b"graph")

    report = verify_bundle(bundle)

    assert report["ok"] is False
    assert report["model_files_found"] == ["vienetts_app/speaker_encoder.onnx"]


def test_release_manifest_is_sorted_and_hashes_files(tmp_path) -> None:
    (tmp_path / "z.zip").write_bytes(b"z")
    (tmp_path / "a.dmg").write_bytes(b"a")

    entries = release_entries([tmp_path / "z.zip", tmp_path / "a.dmg"])

    assert [item["name"] for item in entries] == ["a.dmg", "z.zip"]
    assert all(len(item["sha256"]) == 64 for item in entries)
```

Complete the audit matrix with these assertions:

- a nonexistent bundle root returns a failing report;
- a symlink inside the bundle resolving outside it is rejected;
- a normal small application file passes;
- a nested `speaker_encoder.onnx` is rejected by basename;
- `*.part.wav` is not exempt from the forbidden bundle policy;
- serialized JSON contains only relative paths and no `tmp_path` string.

- [ ] **Step 2: Run focused audit tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_verify_frozen_bundle.py tests/unit/test_release_manifest.py -v`

Expected: FAIL because audit and release-manifest scripts do not exist.

- [ ] **Step 3: Implement bundle audit and release manifest**

Walk only `bundle_root.resolve()` and reject any discovered symlink whose
resolved target is outside it. Emit POSIX relative paths:

```python
relative = path.relative_to(bundle_root).as_posix()
if path.name in FORBIDDEN_MODEL_NAMES or relative.startswith(".cache/huggingface/"):
    forbidden.append(relative)
```

Also reject a regular file larger than 200 MiB whose suffix is `.onnx`, `.data`,
`.bin`, `.safetensors`, `.pt`, or `.npz`; this detects an unlisted accidental
model without treating normal native libraries as model files. The tool exits
nonzero if forbidden data exists.

`release_manifest.py` streams SHA-256 in 1 MiB blocks, sorts only archive
file names, writes valid compact JSON through a temporary file plus
`os.replace`, and makes every entry relative to its supplied input root.

Add this fixed comment to the spec near `datas`:

```python
# Never add official model files here: ModelManager installs verified data under
# the user application data directory, and release CI audits this bundle invariant.
```

- [ ] **Step 4: Run focused audit tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_verify_frozen_bundle.py tests/unit/test_release_manifest.py tests/unit/test_package.py -v`

Expected: PASS.

- [ ] **Step 5: Commit release artifact audit**

```bash
git add scripts/verify_frozen_bundle.py scripts/release_manifest.py \
  packaging/vienetts-app.spec tests/unit/test_verify_frozen_bundle.py \
  tests/unit/test_release_manifest.py tests/unit/test_package.py
git commit -m "build: audit model-free frozen artifacts"
git notes add -m "Phase 6 Task 2: added model-bundle audit and archive checksums."
```

### Task 3: Harden the release workflow with pinned managed-model smoke

**Files:**

- Modify: `.github/workflows/release.yml`
- Modify: `tests/unit/test_linux_packaging.py`
- Modify: `tests/smoke/test_e2e_flows.py`
- Create: `docs/release/managed-model-smoke.md`

**Interfaces:**

- Release workflow environment:

```yaml
VieneuTTSDataRoot: ${{ runner.temp }}/vienetts-app-data
HF_HUB_OFFLINE: "1" # only on the packaged smoke invocation
```

- Required build sequence:
  1. cache/download pinned models into `${data_root}/models`;
  2. validate them through `ModelManager.inspect`;
  3. build the bundle;
  4. audit bundle no-model invariant;
  5. execute packaged `--smoke ... --data-dir "$data_root" --offline`;
  6. validate output WAV;
  7. package artifact and calculate checksums;
  8. upload artifact plus checksum manifest.

- [ ] **Step 1: Write failing workflow-contract tests**

```python
def test_release_workflow_uses_pinned_manifest_download_and_offline_smoke() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "scripts/fetch_models.py --data-dir" in workflow
    assert "--offline" in workflow
    assert "scripts/verify_frozen_bundle.py" in workflow
    assert "HF_HUB_OFFLINE: \"1\"" in workflow
    assert "HF_HOME:" not in workflow


def test_workflow_cache_key_tracks_official_manifest() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "official_model_manifest.py" in workflow
```

Add e2e fake tests that a CLI smoke with an explicit valid managed data
directory completes an artifact and that missing managed paths returns a
nonzero code without downloading.

- [ ] **Step 2: Run focused workflow tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_linux_packaging.py \
  tests/smoke/test_e2e_flows.py -v
```

Expected: FAIL because current release CI uses `HF_HOME` and no bundle audit.

- [ ] **Step 3: Update release CI without exposing secrets**

Replace the current Hub-cache setup with:

```yaml
- name: Restore pinned managed model data
  uses: actions/cache@v4
  with:
    path: ${{ runner.temp }}/vienetts-app-data/models
    key: vienetts-official-model-${{ hashFiles('src/vienetts_app/core/official_model_manifest.py') }}

- name: Install and validate pinned managed model data
  shell: bash
  run: |
    python scripts/fetch_models.py --data-dir "$RUNNER_TEMP/vienetts-app-data"
    python -c "from pathlib import Path; from vienetts_app.core.model_manager import ModelManager; assert ModelManager(Path('$RUNNER_TEMP/vienetts-app-data/models')).inspect().location"
```

Place model setup before the build smoke but after dependencies. It downloads
only on a cache miss through the manifest. Do not preserve/print downloaded
paths outside the runner temp root.

After PyInstaller, add:

```yaml
- name: Verify frozen artifact excludes models
  run: python scripts/verify_frozen_bundle.py --bundle dist/VieNeuTTS

- name: Smoke-test packaged binary with offline managed model
  shell: bash
  env:
    HF_HUB_OFFLINE: "1"
  run: >-
    "${{ matrix.binary }}" --smoke
    "Xin chào, đây là bản kiểm định phần mềm." --voice Adam --stream
    --data-dir "$RUNNER_TEMP/vienetts-app-data" --offline
    -o "$RUNNER_TEMP/smoke.wav"
```

Use the correct bundle argument on macOS:
`dist/VieNeuTTS.app`, or have `verify_frozen_bundle.py` support both app and
one-dir roots. Add an archive checksum step after every package format and
upload its `SHA256SUMS.json` alongside the archive.

- [ ] **Step 4: Run workflow and e2e tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_linux_packaging.py \
  tests/smoke/test_e2e_flows.py -v
```

Expected: PASS.

- [ ] **Step 5: Document local release smoke and commit**

Document:

```bash
python scripts/fetch_models.py --data-dir /tmp/vienetts-ci-data
.venv/bin/python -m PyInstaller packaging/vienetts-app.spec --noconfirm \
  --distpath dist --workpath /tmp/pyi-build
python scripts/verify_frozen_bundle.py --bundle dist/VieNeuTTS
HF_HUB_OFFLINE=1 dist/VieNeuTTS/VieNeuTTS --smoke "Xin chào" --stream \
  --data-dir /tmp/vienetts-ci-data --offline -o /tmp/smoke.wav
python scripts/check_smoke_wav.py /tmp/smoke.wav
```

```bash
git add .github/workflows/release.yml tests/unit/test_linux_packaging.py \
  tests/smoke/test_e2e_flows.py docs/release/managed-model-smoke.md
git commit -m "ci(release): verify offline managed model smoke"
git notes add -m "Phase 6 Task 3: CI smoke-tests the packaged app against pinned managed local models."
```

### Task 4: Make signing/notarization state explicit and credential-gated

**Files:**

- Create: `docs/release/signing-and-notarization.md`
- Modify: `.github/workflows/release.yml`
- Modify: `README.md`
- Modify: `PROJECT_PLAN.md`
- Modify: `tests/unit/test_linux_packaging.py`

**Interfaces:**

- Produces no default signing credentials and no secret values.
- Produces workflow behavior:
  - no configured credential: ad-hoc macOS signing continues and release
    notes/documentation state “unnotarized”;
  - explicit maintainer-enabled credential configuration: the workflow first
    validates all required secret variables are nonempty, imports the
    temporary keychain/certificate, signs with hardened runtime, submits
    notarization, staples only on accepted status, and fails if any action
    fails.
- The maintainer selects the exact GitHub secret names/identities in a
  reviewed follow-up that this plan does not invent.

- [ ] **Step 1: Write failing documentation/workflow tests**

```python
def test_release_docs_do_not_claim_notarization_without_credentials() -> None:
    docs = Path("docs/release/signing-and-notarization.md").read_text(encoding="utf-8")

    assert "human-provided" in docs
    assert "notarized by default" not in docs


def test_workflow_ad_hoc_path_is_explicitly_labeled() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "ad-hoc" in workflow.lower()
    assert "notarization" in workflow.lower()
```

- [ ] **Step 2: Run focused tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_linux_packaging.py -v`

Expected: FAIL because signing/notarization documentation and explicit
workflow label do not exist.

- [ ] **Step 3: Add the human approval boundary**

Create a short document with:

1. current default (ad-hoc signature, not notarized);
2. prerequisite that a release maintainer supplies a valid Apple Developer
   Program identity and app-specific notarization credentials;
3. review requirement for certificate handling, keychain cleanup, workflow
   approval, and a test tag;
4. expected verification commands:

```bash
codesign --verify --deep --strict --verbose=2 VieNeuTTS.app
spctl --assess --type execute --verbose=4 VieNeuTTS.app
xcrun stapler validate VieNeuTTS.app
```

5. explicit warning that `spctl`/stapler results cannot be fabricated on a
   machine without the maintainer’s credentials/notarization ticket.

Update workflow comments and release notes to say that the existing
`codesign --sign -` path is ad-hoc. Do not add a pseudo-notarization step,
secret key name, base64 certificate command, or credentials. A signed release
configuration enters only through an approved follow-up with human-provided
values and a security review.

- [ ] **Step 4: Run focused tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_linux_packaging.py -v`

Expected: PASS.

- [ ] **Step 5: Commit transparent signing state**

```bash
git add docs/release/signing-and-notarization.md .github/workflows/release.yml \
  README.md PROJECT_PLAN.md tests/unit/test_linux_packaging.py
git commit -m "docs(release): document signing credential boundary"
git notes add -m "Phase 6 Task 4: documented ad-hoc default and human-required notarization process."
```

### Task 5: Add evidence-only upstream experiment gates

**Files:**

- Create: `scripts/experiments/measure_sdk_sessions.py`
- Create: `scripts/experiments/compare_onnx_artifacts.py`
- Create: `scripts/experiments/evaluate_experiment.py`
- Create: `tests/unit/test_experiment_gates.py`
- Modify: `docs/performance/README.md`
- Modify: `PROJECT_PLAN.md`

**Interfaces:**

- Produces JSONL records with:

```json
{
  "schema_version": 1,
  "experiment": "sdk-lazy-session",
  "candidate": "reference",
  "platform": "darwin-arm64",
  "model_revision": "pinned-revision",
  "mode": "stream",
  "output": {
    "sample_rate": 48000,
    "frames": 1234,
    "sha256": "public-fixture-output-hash"
  },
  "metrics": {
    "initialize_ms": 100.0,
    "peak_rss_bytes": 123456,
    "first_chunk_ms": 80.0
  }
}
```

- `evaluate_experiment.py` returns zero only when all required records pass:

```python
@dataclass(frozen=True)
class ExperimentGate:
    required_platforms: tuple[str, ...]
    max_output_rmse: float
    min_output_correlation: float
    max_init_regression_ratio: float
    max_rss_regression_ratio: float
```

- Initial stable gate values:
  - same sample rate and nonzero valid WAV frames;
  - correlation at least `0.999`;
  - RMSE at most `0.001`;
  - candidate initialization and peak RSS each no worse than reference by
    more than 5%;
  - records from macOS arm64, Windows x64, and Linux x64 before a production
    change is eligible.

- [ ] **Step 1: Write failing gate tests**

```python
def test_experiment_gate_rejects_missing_platform_and_output_drift(tmp_path) -> None:
    write_records(
        tmp_path / "records.jsonl",
        [
            record(platform="darwin-arm64", correlation=0.9999, rmse=0.0),
            record(platform="linux-x64", correlation=0.90, rmse=0.1),
        ],
    )

    result = evaluate(tmp_path / "records.jsonl", default_gate())

    assert result.passed is False
    assert "windows-x64" in result.failures
    assert "output" in result.failures["linux-x64"]


def test_gate_rejects_init_and_rss_regressions() -> None:
    result = evaluate_records(
        [record(platform=platform, initialize_ms=106, reference_initialize_ms=100,
                peak_rss_bytes=106, reference_peak_rss_bytes=100)
         for platform in ("darwin-arm64", "windows-x64", "linux-x64")],
        default_gate(),
    )

    assert result.passed is False
```

Complete the experiment-gate matrix with these assertions:

- valid evidence for darwin-arm64, windows-x64, and linux-x64 passes;
- malformed JSON and every required missing field fail with a field-specific
  error;
- an input record containing a private field such as `voice_path` is
  rejected;
- an empty output hash is rejected;
- exactly 5% initialization and RSS regressions pass while 5.01% fails;
- missing pinned manifest returns an error before the fake local candidate
  engine factory is called.

- [ ] **Step 2: Run focused experiment tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_experiment_gates.py -v`

Expected: FAIL because experiment scripts and gate evaluator do not exist.

- [ ] **Step 3: Implement session observation experiment**

`measure_sdk_sessions.py` receives explicit
`--data-dir`, `--mode stream|infer|denoise`, `--iterations`, and `--output`.
It validates the managed reference install first, then instruments only
observable boundaries:

```python
started = time.perf_counter_ns()
engine = TTSEngine(backend="onnx", managed_model=location)
engine.initialize()
record_metric("initialize_ms", elapsed_ms(started))
```

For each mode, run a fixed public fixture from the benchmark corpus,
write/validate only a temporary artifact, capture numeric output statistics
and hash, then remove the temporary file. It must neither monkey-patch SDK
internals nor claim that an unseen private session is lazy.

- [ ] **Step 4: Implement comparison and gate evaluator**

`compare_onnx_artifacts.py` requires both explicit reference and candidate
directories. It computes output correlation/RMSE only on generated numerical
arrays in process memory, emits their hashes and aggregate statistics, and
does not write raw audio to the report. It refuses candidate files outside
the caller-provided experiment directory.

`evaluate_experiment.py` rejects absent/malformed expected platforms,
different sample rates, missing nonzero frames, drift, init/RSS regression,
or any field named `text`, `voice`, `path`, `audio`, `token`, or `credential`.
It writes a single summary JSON only under a caller-specified output path.

- [ ] **Step 5: Run focused experiment tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_experiment_gates.py -v`

Expected: PASS.

- [ ] **Step 6: Document exact production gate and commit**

Document that these are experiments, not a performance feature. Include:

```bash
.venv/bin/python scripts/experiments/measure_sdk_sessions.py \
  --data-dir /path/to/test-data --mode stream --iterations 5 \
  --output /tmp/sdk-reference.jsonl
.venv/bin/python scripts/experiments/evaluate_experiment.py \
  /tmp/cross-platform-records.jsonl --output /tmp/gate.json
```

State that a passing gate requires a design update and explicit user
authorization before modifying `TTSEngine`, model manifest, dependencies, or
frozen packaging.

```bash
git add scripts/experiments/measure_sdk_sessions.py \
  scripts/experiments/compare_onnx_artifacts.py \
  scripts/experiments/evaluate_experiment.py \
  tests/unit/test_experiment_gates.py docs/performance/README.md PROJECT_PLAN.md
git commit -m "test(perf): gate optional upstream experiments"
git notes add -m "Phase 6 Task 5: added evidence-only SDK and ONNX experiment gates."
```

### Task 6: Execute final release-readiness verification

**Files:**

- Modify: `README.md`
- Modify: `PROJECT_PLAN.md`
- Modify: `docs/release/managed-model-smoke.md`
- Modify: `docs/performance/README.md`

**Interfaces:**

- Consumes all Phase 1–6 contracts.
- Produces a local release-readiness report identifying:
  - test/ruff results;
  - model-free bundle audit result;
  - managed local offline smoke result;
  - archive checksum manifest;
  - supported/not-yet-provisioned signing status;
  - benchmark methodology only, not unverified target claims.

- [ ] **Step 1: Add a final end-to-end test**

```python
def test_packaged_contract_uses_validated_local_model_without_hub(tmp_path) -> None:
    data_dir = tmp_path / "app-data"
    install_managed_fixture(data_dir)
    bundle = create_fake_model_free_bundle(tmp_path)

    assert verify_bundle(bundle)["ok"] is True
    assert run_smoke(
        "Xin chào, đây là bản kiểm định phần mềm.",
        "Adam",
        tmp_path / "smoke.wav",
        data_dir=data_dir,
        offline=True,
        engine_factory=FakeLocalOnlyEngine,
    ) == 0
```

- [ ] **Step 2: Run Phase 6 focused suite**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_model_manager.py \
  tests/unit/test_fetch_models.py \
  tests/unit/test_app_entry.py \
  tests/unit/test_verify_frozen_bundle.py \
  tests/unit/test_release_manifest.py \
  tests/unit/test_linux_packaging.py \
  tests/unit/test_experiment_gates.py \
  tests/smoke/test_e2e_flows.py -v
```

Expected: PASS without a live model Hub request.

- [ ] **Step 3: Run local bundled artifact validation when dependencies/models are available**

Run:

```bash
rm -rf /tmp/vienetts-release-smoke-data /tmp/vienetts-pyi-build
python scripts/fetch_models.py --data-dir /tmp/vienetts-release-smoke-data
.venv/bin/python -m PyInstaller packaging/vienetts-app.spec --noconfirm \
  --distpath /tmp/vienetts-dist --workpath /tmp/vienetts-pyi-build
.venv/bin/python scripts/verify_frozen_bundle.py --bundle /tmp/vienetts-dist/VieNeuTTS.app
HF_HUB_OFFLINE=1 /tmp/vienetts-dist/VieNeuTTS.app/Contents/MacOS/VieNeuTTS \
  --smoke "Xin chào, đây là bản kiểm định phần mềm." --voice Adam --stream \
  --data-dir /tmp/vienetts-release-smoke-data --offline -o /tmp/vienetts-smoke.wav
.venv/bin/python scripts/check_smoke_wav.py /tmp/vienetts-smoke.wav
```

Expected: all commands exit 0 on macOS arm64. On another development OS,
replace the app/binary path according to its one-dir layout. Do not run this
step without approval when it would download the 327 MB model set; record it
as skipped rather than fabricating success.

- [ ] **Step 4: Run the complete quality gate**

Run:

```bash
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest
git diff --check
git status --short
```

Expected: all quality commands exit 0. `git status` is inspected before any
commit; it must not be used to overwrite unrelated changes.

- [ ] **Step 5: Update release documentation and commit**

Document the distinction between application download, one-time model setup,
offline operation after validation, model-free updates, supported smoke
verification, and optional signing. Cite real local verification output only
after it exists.

```bash
git add README.md PROJECT_PLAN.md docs/release/managed-model-smoke.md \
  docs/performance/README.md
git commit -m "docs(release): record model-free offline smoke contract"
git notes add -m "Phase 6 Task 6: completed release-readiness verification and truthful documentation."
```
