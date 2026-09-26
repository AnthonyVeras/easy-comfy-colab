# English quick start

Easy Comfy Colab runs the desktop controller on Windows and **ComfyUI inference on Google Colab**. Model files, inputs, workflows and outputs persist in your Google Drive. It uses the official Colab CLI inside WSL2 and the ComfyUI-Easy-Install project for remote setup.

This is an experimental community release. It does not include Google credentials, model weights or compute credits. G4 allocation, real cross-account Drive copying and a complete installation on other machines still need community validation.

## Requirements

- Windows with WSL2 / Ubuntu 24.04, initialized with your Linux user.
- A Google account with Colab access and enough Drive storage.
- Python 3.11+ on Windows when running from source. Prebuilt packages only require the WSL setup.

## Install from source

```powershell
git clone https://github.com/AnthonyVeras/easy-comfy-colab.git
cd easy-comfy-colab
.\Setup.ps1
.\Launch.ps1
```

`Setup.ps1 -Distro Ubuntu -WslUser your_linux_user` selects a different WSL environment. Setup creates a dedicated CLI environment and SSH key. It does not start a Colab runtime. Sign in through the application using your own Google account.

For a Windows release ZIP, extract the whole package and run `Setup.ps1 -SkipWindowsDependencies`, then `Launch.ps1`. Do not move the executable away from its `_internal` folder or the project scripts. `Create-Shortcut.ps1` creates a desktop shortcut after a build/release extraction.

## Main tabs (Portuguese UI)

- **Sessão:** select hardware, start/stop the VM, open/restart ComfyUI, reconnect, unload VRAM.
- **Modelos:** paste Hugging Face/Civitai file URLs, choose destination category and parallel downloads. Use `URL | filename.safetensors` for Civitai links.
- **Contas e Drive:** add accounts, copy a project folder on Google's servers or share the models library. Copies preserve the source. Sharing grants edit access and depends on the source owner.
- **Monitor:** local and remote resource usage.
- **Workflows:** inspect missing nodes/model references without executing a workflow.
- **Histórico:** observed operation history and estimated compute usage.
- **Configurações:** provider credentials, balance warning, optional idle shutdown.

The local ComfyUI URL is `http://127.0.0.1:18188/`. The optional Comfy MCP endpoint is `http://127.0.0.1:18189/mcp`; configure it in your own compatible MCP client. Both require a running GPU session. CPU downloads mode starts only the downloader.

**Closing the browser does not stop billing.** Use **Encerrar VM**. Usage rate is account-wide, not a guaranteed per-VM bill. Remote installation files are temporary; Drive data survives. The application is not a way to extend Windows RAM with cloud memory.

See the main [README](../README.md), [architecture](ARCHITECTURE.md), [troubleshooting](TROUBLESHOOTING.md), [security policy](../SECURITY.md) and [license notices](../THIRD_PARTY_NOTICES.md) for details.

## Save outputs to your PC (2.0.2)

Choose **Meu PC** under **Sessão → Onde salvar os resultados** before starting. For an existing session, **Reiniciar ComfyUI** applies the choice by restarting only the server; the Colab VM stays allocated. Merely selecting the option does not restart anything.

Outputs stay temporarily on the VM and are copied to `%USERPROFILE%/Comfy Colab Results/output/<VM ID>/` every 15 seconds while the app is open and the queue is idle. The local `input` folder sits alongside `output`. Other accounts have separate subfolders. Before shutting down, the app pauses the idle server and requires a verified final copy; a failed transfer leaves the VM running and attempts to resume the server.

Input uploads and workflows still use Drive. Custom nodes with their own save paths may bypass this setting. Closing the app stops automatic copying; termination from the Colab website or runtime loss can destroy files not yet copied. Existing Drive files are not moved or deleted.

## Installation images and model cache (2.0.5)

The first GPU session installs the environment and prepares a private archive in your own Drive. Compatible later sessions restore it onto the VM's local disk after integrity checks. The public ZIP contains no prebuilt remote image, models or accounts. A changed Colab Python/PyTorch base can require another full installation.

Use **Sessão → Atualizar imagem do Drive** to save newly installed nodes and dependencies without restarting ComfyUI. **Encerrar VM** performs the same save before shutting down. A failed save keeps the VM running and consuming credits; inspect the activity log and retry. Updates can add several minutes to shutdown. Exiting with the VM still running does not save the image; forced Colab termination cannot run this protection.

**Modelos → Carregamento rápido** offers a temporary VM disk cache and optional bounded RAM pre-reading. Select files manually or from a workflow, then prepare them. Original models remain on Drive. RAM pre-reading uses the Linux page cache, not pinned memory or preloaded VRAM. Its first transfer still takes time.

The reference installation's user reported a reduction from about 17 to 4 minutes. This is an observed result, not a startup-time guarantee. See [validation](VALIDATION.md).

## App language (2.0.6)

Open **Configurações → Idioma / Language** and choose **English**. The interface switches immediately, remembers your choice, and preserves the active session and form fields. No ComfyUI or VM restart is needed. Remote technical logs keep their original language; configure the ComfyUI editor language separately in ComfyUI.
