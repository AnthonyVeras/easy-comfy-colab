# Easy Comfy Colab 2

A new Windows application for managing ComfyUI in Google Colab. Native account/session control, an accessible dark React interface, remote model/node management and verified output delivery. The ComfyUI editor opens in your browser; inference stays on the VM.

This is a progressive rebuild. Implementation and evidence are tracked by cycle in `docs/STATUS.md`; a local build does not establish a three-minute Colab startup or certify an installation on a clean Windows machine.

## Development

Use Python 3.12+ and Node.js. Install the pinned controller requirements into a virtual environment, install frontend dependencies with `npm ci`, then use the package scripts for typechecking, tests, browser preview and desktop wrappers. Controller and remote tests use temporary files/mocks and do not allocate VMs.

```powershell
python -m unittest discover -s controller/tests -v
python -m unittest discover -s remote/tests -v
npm run typecheck
npm test
npm run models:test
npm run build
```

The browser preview is explicitly offline. No session, account balance or successful installation is simulated as live data. Tauri and Electron wrap the same frontend and native controller for the performance comparison.

## Data and safety

The new application's data and runtime are separate from the legacy project. Normal operation does not require WSL, a global Python installation or the source checkout. A private one-time migration can import a user's legacy Google/Drive/Hugging Face/Civitai credentials, preserving the originals and OAuth issuer; personal data never ships in the installer.

The controller binds each Google identity to its exact allocation and authenticated transport. It never adopts an unknown VM by GPU/port. Pending unverified PC output copies or runtime preservation failures prevent VM shutdown. Reconnecting does not allocate another VM.

## Project

One active session, multiple accounts. Drive/PC output destinations, CPU download sessions, resumable downloads, categorized library, node packages, workflow checks, model cache, Drive copy/sharing, monitoring, history and conservative idle protection form the V1 checklist. Telegram and Huihui are outside V1.

See `docs/CONTRACT.md`, `docs/ROADMAP.md`, `docs/STATUS.md` and `docs/BENCHMARK.md`. The local preview uses Tauri after both 0.1.0 wrappers passed the agreed desktop budgets. Version 0.1.1 adds the VM download bridge and an Easy Install base. Public release and clean-Windows certification remain pending. The detailed development vault uses a separate MCPVault connection; see `tools/vault.py`. No automation or external calendar is created.

## ComfyUI downloads and base

ComfyUI's **Download** and **Download All** missing-model buttons queue work on the owned VM. They use the same resumable HF/Civitai queue as the app, preserving model categories in the new Drive library. A ComfyUI extension shows remote progress and refreshes loader choices after completion. A disconnected or rejected request shows recovery; it never falls back to saving the weights on Windows. Provider credentials remain outside the browser.

The runtime uses the reviewed [ComfyUI-Easy-Install MAC-Linux base](https://github.com/Tavris1/ComfyUI-Easy-Install/tree/fa3882893d083dc116ec0c62b9fdcd4ebc91397e): versioned node packages and shipped workflow examples, with the Colab Python/Torch/CUDA stack preserved. The desktop installer script is inspected rather than executed wholesale. `remote/runtime.json` records exact package sources, versions, hashes and compatibility limits. Pending packages are identified in the app; source-only dependencies need a separately tested Linux build before activation. This change creates a new artifact recipe and preserves earlier Drive artifacts.

New code is MIT; third-party code and dependencies retain their own licenses. A public release needs the project's Google OAuth configuration and applicable verification. GitHub publication is a separate step.
