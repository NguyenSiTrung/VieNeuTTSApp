# Optional Qwen Engine Pack — Setup & Deployment Guide

VieNeu-TTS includes an **optional multilingual engine pack** powered by official [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) 0.6B models (`qwen-tts`).

The default application installation is **100% torch-free and lightweight**: VieNeu-TTS v3 Turbo runs fully offline on CPU via ONNX Runtime. Nothing Qwen-related is downloaded, imported, or executed unless you explicitly opt in.

---

## 1. Profiles & Capabilities

The pack provides two selectable model profiles:

| Profile | Model ID | Download Size | Capabilities | Language Support |
| :--- | :--- | :--- | :--- | :--- |
| **Qwen CustomVoice** | `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` | ~1.5 GB | 9 preset voices, natural-language style & emotion instructions (`instruct`) | 10 languages (`en`, `zh`, `ja`, `ko`, `de`, `fr`, `ru`, `es`, `it`, `pt`) — **no Vietnamese** |
| **Qwen Base** | `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | ~1.5 GB | Reference-audio voice cloning (3–8s clip + transcript), engine-isolated saved voices | Same 10 languages — **no Vietnamese** |

> ⚠️ **Vietnamese Synthesis:** Qwen does **not** support Vietnamese. VieNeu-TTS v3 Turbo remains the default and recommended engine for Vietnamese. Choosing Vietnamese with a Qwen backend surfaces an actionable error directing you to VieNeu.

---

## 2. Hardware Requirements

- **GPU (Recommended):** **NVIDIA GPU** with CUDA 12.8+ driver support (`nvidia-smi`) and at least **4 GB VRAM**. Synthesis on CUDA achieves comfortable real-time playback.
- **CPU (Fallback):** Functional on x86_64, Intel Arc / AMD graphics, and Apple Silicon (via PyTorch CPU wheels), but compute-intensive and slower than real-time.
- **Disk Space:**
  - Runtime: ~2.5 GB (PyTorch, Transformers, `qwen-tts`).
  - Models: ~1.5 GB (CustomVoice only) or ~3.0 GB (CustomVoice + Base).

> 📦 **Packaged / Standalone Releases:** Pre-compiled standalone application binaries (Linux `.tar.gz`, Windows `.zip`, macOS `.dmg`) exclude PyTorch by design to preserve the lightweight CPU footprint. Running the Qwen engine requires launching from a Python virtual environment (`.venv`).

---

## 3. Prerequisites & Environment Setup

### Avoid `error: externally-managed-environment` (PEP 668)
On modern Linux (Ubuntu 24.04+, Debian 12+) and macOS with Homebrew Python, running bare `pip install` against system Python is blocked by the OS. **All installations must be run inside the virtual environment (`.venv`)**.

### Linux System Audio Package (`sox`)
`qwen-tts` uses `sox` for waveform normalization. On Debian/Ubuntu, install `sox` before running Qwen:
```bash
sudo apt update && sudo apt install -y sox libsox-fmt-all
```
*(On Windows and macOS, audio libraries handle this automatically).*

---

## 4. Step 1: Install Runtime Dependencies

Install the `[qwen]` optional dependencies into your project environment (`.venv`).

### Option A: Using `uv` (Recommended — Fastest & Auto-Detects `.venv`)

If you have `uv` installed:

**CPU-only (Intel Arc, AMD, Apple Silicon, or CPU):**
```bash
uv pip install -e ".[qwen]"
```

**NVIDIA GPU (CUDA 12.8 Acceleration):**
```bash
uv pip install -e ".[qwen]" && uv pip install --index-url https://download.pytorch.org/whl/cu128 "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128"
```

---

### Option B: Using Standard `pip` (Requires Activating `.venv`)

**1. Activate the virtual environment:**

- **Linux / macOS:**
  ```bash
  source .venv/bin/activate
  ```
- **Windows (PowerShell):**
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
- **Windows (Command Prompt):**
  ```cmd
  .venv\Scripts\activate.bat
  ```

**2. Install dependencies:**

- **CPU-only:**
  ```bash
  pip install -e ".[qwen]"
  ```
- **NVIDIA GPU (CUDA 12.8 Acceleration):**
  ```bash
  pip install -e ".[qwen]"
  pip install --index-url https://download.pytorch.org/whl/cu128 "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128"
  ```

---

## 5. Step 2: Verify Runtime Installation

Verify that the Qwen runtime and PyTorch import cleanly before opening the application:

```bash
.venv/bin/python -c "import qwen_tts, torch; print('✓ Qwen runtime ready | PyTorch:', torch.__version__, '| CUDA available:', torch.cuda.is_available())"
```

Expected output:
- **CPU mode:** `✓ Qwen runtime ready | PyTorch: 2.8.0... | CUDA available: False`
- **NVIDIA CUDA mode:** `✓ Qwen runtime ready | PyTorch: 2.8.0+cu128 | CUDA available: True`

---

## 6. Step 3: Download Model Checkpoints

You can download checkpoints either directly inside the app wizard or via the command-line fetch script.

### Method A: In-App Setup Wizard (Recommended)

1. Launch the application:
   ```bash
   .venv/bin/vienetts-app
   ```
2. Navigate to **Cài đặt (Settings)** → **Gói Qwen tùy chọn (Optional Qwen pack)** card.
3. Click **Thiết lập từng bước… (Step-by-step setup…)**.
4. The 3-stage wizard guides you:
   - **Stage 1 (Chọn gói):** Pick **CustomVoice** (1.5 GB, 9 preset voices) or **CustomVoice + Base** (3 GB, includes cloning).
   - **Stage 2 (Môi trường):** Select **CPU** or **NVIDIA CUDA 12.8**.
   - **Stage 3 (Cài đặt):** Click **Tải CustomVoice** and/or **Tải Base**. Progress is displayed live with Cancel and Retry support. Click **Kiểm tra lại (Refresh)** after running commands in the terminal.

---

### Method B: Terminal Fetch Script

You can pre-fetch checkpoints before opening the GUI:

```bash
# Download both CustomVoice and Base (~3.0 GB total)
.venv/bin/python scripts/fetch_qwen_models.py

# Or download ONLY CustomVoice (~1.5 GB, fixed voices, no cloning)
.venv/bin/python scripts/fetch_qwen_models.py --repo "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"

# Or download ONLY Base (~1.5 GB, voice cloning)
.venv/bin/python scripts/fetch_qwen_models.py --repo "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
```

---

## 7. Air-Gapped & Offline Deployment

Qwen models are resolved via the standard Hugging Face hub cache layout (`models--<owner>--<repo>/snapshots/*`). To set up an air-gapped or offline machine:

1. **On an internet-connected machine:**
   Download the models into a dedicated directory (e.g. on a USB drive):
   ```bash
   HF_HOME=/media/usb/qwen-cache .venv/bin/python scripts/fetch_qwen_models.py
   ```
2. **Transfer to the target offline machine:**
   Copy the directory or mount the drive on the offline machine. The structure looks like:
   ```text
   /media/usb/qwen-cache/
   └── hub/
       ├── models--Qwen--Qwen3-TTS-12Hz-0.6B-CustomVoice/
       │   └── snapshots/<hash>/...
       └── models--Qwen--Qwen3-TTS-12Hz-0.6B-Base/
           └── snapshots/<hash>/...
   ```
3. **Launch the app with `HF_HOME` set:**
   ```bash
   HF_HOME=/media/usb/qwen-cache .venv/bin/vienetts-app
   ```
   The Settings tab will inspect the cache and mark `✓ Checkpoint CustomVoice` and `✓ Checkpoint Base` as ready. Synthesis will run 100% offline without network calls.

---

## 8. Using Qwen in VieNeuTTS Studios

Once the pack is ready:

1. **Select Engine:** In the Text, Paragraph, or Audiobook Studio, open the **Engine** dropdown and choose:
   - `Qwen3-TTS CustomVoice`
   - `Qwen3-TTS Base`
2. **Select Language:** Choose any of the 10 supported languages (e.g. English, Japanese, Chinese).
3. **Select Voice:**
   - For **CustomVoice**: pick from the 9 official speakers (`Vivian`, `Serena`, `Uncle_Martin`, `Dylan`, `Eric`, `Ryan`, `Aiden`, `Ono_Rei`, `Sohee`).
   - For **Base**: select an enrolled cloned voice or enroll a new voice in the **Nhân bản (Voice Cloning)** tab.
4. **Style / Emotion Instructions (`instruct`):**
   When using CustomVoice, an **Instruction** field appears. You can enter natural language prompts describing tone, mood, or pacing, such as:
   - `"Speak with cheerful energy and quick cadence."`
   - `"Whisper mysteriously with dramatic pauses."`
5. **Audio Output:** All Qwen 24 kHz output is automatically and statefully resampled to 48 kHz mono float32 to integrate seamlessly with VieNeuTTS audio players, WAV exporters, and audiobook chapter caches.

---

## 9. Troubleshooting & FAQs

### Q: Why do I get `error: externally-managed-environment` when running `pip install`?
Ubuntu 24.04 and Debian 12+ enforce PEP 668 to prevent `pip` from altering system packages. You must install inside the project's virtual environment:
```bash
# Using uv:
uv pip install -e ".[qwen]"
# Or using pip after activation:
source .venv/bin/activate && pip install -e ".[qwen]"
```

### Q: Why does the status list show `torch (cpu)` even though I have an NVIDIA GPU?
You installed PyTorch from PyPI's default index, which provides CPU-only wheels on Linux. Run:
```bash
uv pip install --index-url https://download.pytorch.org/whl/cu128 "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128"
```
Then restart the app or click **Kiểm tra lại (Refresh)** in the setup wizard.

### Q: I have an Intel Arc or AMD Radeon GPU. Can I use CUDA 12.8?
No. CUDA is proprietary to NVIDIA hardware. For Intel Arc, AMD Radeon, or Apple Silicon, use the **CPU runtime**. The app will run on CPU without requiring CUDA.

### Q: I see `SoX could not be found!` in terminal output on Linux.
`qwen-tts` uses SoX for certain audio operations. Install it via apt:
```bash
sudo apt install -y sox libsox-fmt-all
```

### Q: Why does Qwen Base voice cloning ask for a transcript?
Unlike VieNeu's acoustic-only embedding extraction, Qwen3-TTS Base's architecture requires both a 3–8 second clean audio sample **and its exact text transcript** (`ref_text`) to condition the language model backbone. Both are mandatory for high-quality voice enrollment.

### Q: Checkpoint download was interrupted. Do I have to start over?
No. Hugging Face `snapshot_download` supports automatic resume. Simply click **Tải** again in the wizard or re-run `.venv/bin/python scripts/fetch_qwen_models.py`; already-verified chunks are reused.
