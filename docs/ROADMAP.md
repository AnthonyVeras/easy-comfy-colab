# Work calendar by acceptance gates

Start date: 2026-10-08, America/Sao_Paulo. Dates are not invented delivery promises. User tests are scheduled when a build can exercise the corresponding journey.

| Cycle | Deliverable | Acceptance |
| --- | --- | --- |
| 0 | Separate repository, MCP vault, audit, contracts | Notes written and read through dedicated MCP; legacy preserved |
| 1 | Native Windows controller and private import | Isolated identity, allocation ownership, Drive, SSH/SFTP/tunnels, reconnect and safe stop |
| 2 | Shared dark UI and Tauri/Electron wrappers | Release builds, progressive journeys, accessibility and measured comparison |
| 3 | New minimal runtime | Open ComfyUI, generate, deliver and stop safely |
| 4 | Models, nodes, workflows and output management | Cancel/resume, conflicts, compatibility and failed copies preserve data |
| 5 | Advanced parity | CPU downloads, SSD/RAM cache, Drive sharing/copy, monitor/history/idle |
| 6 | Optimization and V1 beta | Clean install, accessibility, three valid new starts <=180s, OAuth/licensing/release gates |

Framework budgets: <=250 MiB peak private memory during idle measurement for the whole process tree, <=1% mean total machine CPU, <=3s local opening. Compare the same release UI/controller/machine/state. Prefer lower memory among approved candidates; differences below 10% favor Tauri. Both failing requires correction.

Remote timing starts at the click and ends at usable ComfyUI through the tunnel after initial setup with a compatible newly-built artifact. Report all phases and human waits. Initial installation, MCP readiness and first generation are separate measurements. External provisioning/authorization cannot be represented as a universal guarantee.
