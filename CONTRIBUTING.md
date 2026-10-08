# Contributing

Read AGENTS.md and docs/CONTRACT.md first. Keep changes within the relevant subsystem and update its tests. Do not add user-specific paths, credentials, personal integration code or private fixture outputs.

Use exact account/allocation identity; unknown ownership never authorizes shutdown. Preserve verified outputs and the last good runtime. Keep blocking remote I/O outside UI threads. Prefer upstream APIs and standard ComfyUI path configuration over patches of global loader state.

Report which checks are local/mocked and which use a real VM. A successful build does not prove remote performance. Update the project documentation and acceptance checklist before claiming a cycle complete.
