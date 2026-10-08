# Easy Comfy Colab 2

This is the new community project. The legacy checkout is a read-only reference. Never adopt, restart, stop, or modify a legacy VM or runtime image.

- Windows native controller; no WSL in normal operation. One active session and one controller per Windows user.
- Bind Google subject, account, allocation endpoint and transport. Never infer ownership from GPU, HTTP port, or a baseline difference.
- Never stop a VM while output delivery or required runtime preservation is unverified. Unknown ownership/state preserves the VM and reports recovery.
- Secrets stay in DPAPI storage/controller memory, outside renderer, process arguments, repository, logs, diagnostics and Obsidian.
- New runtime, Drive root and local storage are separate. Import legacy credentials only on explicit authorization, preserving sources and OAuth client binding. Telegram and Huihui are excluded.
- UI: React/TypeScript, Cobalt-inspired monochrome, monospace, dark default, accessible labels/focus, PT-BR and English.
- Avoid unrelated options and generic plugin frameworks. Use upstream components with pinned revisions and notices.
- Before workflow execution, call the new VM's official ComfyUI MCP `server_info` and verify the owned session, `/content/easy-comfy-colab-2/ComfyUI`, running server and received GPU. Never use legacy ports 18188/18189 or a local ComfyUI installation. MCP paths are Linux VM paths.

## Documentation is part of each task

Use the dedicated Obsidian MCP, never the unrelated existing vault. Development fallback: `python tools/vault.py read "00 - Painel.md"` connects to the configured MCPVault server through MCP stdio. Local vault configuration is in ignored `.private/vault.json`.

Read the panel and relevant feature/decision notes before a task. At completion, update the feature, tests and handoff notes through MCP, then read them back. Record implementation, evidence, limits and next steps. No secrets or raw private logs. Parallel contributors own separate notes; the primary agent serializes panel updates.

## Verification

Controller: `python -m unittest discover -s controller/tests -v`. Runtime: `python -m unittest discover -s remote/tests -v`. UI: package typecheck/build and focused behavior checks. Real VM benchmarks must report complete elapsed time and human authorization, never omit unsuccessful runs.
