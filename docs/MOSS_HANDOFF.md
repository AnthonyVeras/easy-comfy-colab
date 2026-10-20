# MOSS training handoff

Implemented user flow and limitations: [MOSS_TRAINING.md](MOSS_TRAINING.md). Controller contract: [CONTRACT.md](CONTRACT.md). Evidence: [STATUS.md](STATUS.md).

Next real-session validation: configure the user’s Google Desktop OAuth JSON; authorize account and Drive through the existing app; explicitly select/start an owned GPU VM; verify the received accelerator and NVIDIA driver; upload a small captioned dataset; prepare, start, stop after a complete checkpoint, reconnect and resume; verify final adapter export and normal safe shutdown. Record complete wall time and unsuccessful attempts. Do not allocate a cloud VM without user authorization. A full 150-image run follows only after the small recovery test succeeds.

Use the app’s existing Google Desktop OAuth configuration for native login. The test build includes no client JSON, credentials, account database, photos or model weights. It is an evaluation build, not a verified cloud-training release. The new private Drive dataset is never published to GitHub.

The dedicated Obsidian MCP fallback lacked `.private/vault.json`; repository notes were updated instead. Import these notes through the dedicated development vault when its configuration is restored.
