# Native controller

Run `python controller/main.py`; stdin/stdout are UTF-8 JSONL only. The desktop
shell owns the process and sends the actions in `docs/CONTRACT.md`. Closing
stdin disconnects transports and waits for allocation workers before releasing
the Windows singleton. It does not unassign a VM. A recorded notebook can be
recovered by exact identifier with a read-only Google assignment request.

Drive mounting emits an `auth` event with `purpose: mount_drive` and
`needs_confirmation: true` when browser consent is required. After granting
access, submit `account.submitCode` with `account_id`, `session_id`,
`purpose: mount_drive`, and `confirmed: true`. This carries no authorization
code and is the only confirmation allowed while startup holds its operation
lock. It is bound to the active account/session and pending mount. The remote
mount waits up to 600 seconds; errors receive a correlated `colab_reply`
immediately. An incomplete startup resumes through `session.reconnect` using
the exact recorded notebook; do not call `session.start` to recover it.

Install `controller/requirements.lock.txt` into the project venv on Windows
Python 3.14. This pins the runtime dependency closure separately from build
tools. The upstream Google modules are lazily loaded from `controller/vendor`;
its manifest contains the revision and hashes. Google, SSH and remote modules
are not loaded by an idle snapshot. PyInstaller resources resolve relative to
`sys._MEIPASS`; package `remote/` and the vendor notices there.

Local state uses `%LOCALAPPDATA%/EasyComfyColab2/state.sqlite3`. Secrets are
Windows DPAPI blobs, bound to the Windows user. `ECC_DATA_DIR` explicitly
overrides the state root for isolated tests and benchmarks. Never point an
isolated benchmark at the user's state. `EASY_COMFY_OAUTH_CLIENT` selects a
private Google Desktop OAuth client JSON for fresh onboarding; the default
development path is `.private/oauth-client.json`. No personal OAuth client is
shipped. Colab and full Drive grants are separate and must have the same Google
subject. Browser callbacks use PKCE and state verification. Copy-code private
clients and provider credentials use the native masked Windows dialog.

Private import is explicitly invoked with:

```
python controller/main.py --import-legacy --legacy-home <verified-UNC-home>
```

Optional `--legacy-windows-root` chooses old Windows metadata;
`--selected-only` imports the selected profile. Import preserves source files,
verifies Google subject/email, encrypts the destination and retains the original
OAuth client binding. It does not import active sessions, runtime images, SSH
keys, Telegram or models/workflows. UNC access may activate WSL for this
one-time source read; normal product operation uses native Windows networking.
Import output contains only account metadata, counts and classified errors.
Revoked independent Drive grants require new authorization.

Native normal operation preflights exclusive loopback ports 28188/28190/28189,
allocates one notebook, mounts Drive through the pinned runtime hook and opens
one SSH-over-WebSocket transport. ComfyUI, companion and official MCP share that
transport. Ownership includes account subject, notebook, endpoint, runtime
nonce and SSH fingerprint. Readiness calls official MCP `server_info` and checks
the new workspace, in addition to ComfyUI and companion identity.

PC stop hashes every output after SFTP delivery and durable local write. Drive
stop acknowledges the mounted Drive manifest without copying it to the PC.
Both modes then preserve/verify the runtime and request a final remote shutdown
gate before exact-endpoint unassignment. Every failure preserves the VM and
attempts to resume its admission gate. Unknown idle metrics suspend automatic
stop. Native imports and unit tests do not allocate real VMs.

Local monitoring samples real psutil CPU counters, available RAM and controller
RSS no more than once every five seconds. The first CPU observation and failed
readings stay unknown. Local readings remain separate from VM metrics.
`history.list` returns a consistent count and page, optionally filtered by
account, session or operation; receipts retain the latest 2,000 operations and
only classified results, identifiers, durations and error codes. Request
parameters, secret URLs and arbitrary exception text are never recorded.

`download.list`, `workflow.list`, `node.catalog` and `node.versions` delegate
bounded pages to the owned companion. Saved workflow checks accept only a
relative JSON path under the new Drive workflow root. Download pagination
never proves idleness: automatic stop requires the companion's aggregate
`downloads.busy` to be explicitly false, plus known idle queue/cache state.

Poll results remain local until the companion, account, session and ready state
are revalidated after every network response. Committing metrics and copying
outputs share the operation mutex with account/session changes. Stale results
are discarded, including delayed balance failures and old automatic stop
requests. `ECC_BENCHMARK=1` permits only local snapshot, preferences, diagnostics
and history actions; it never polls cloud services or imports accounts.

PC outputs are delivered in batches of up to 100 stable files while the known
generation queue is idle. SFTP writes are verified by size and SHA-256 and made
durable before a live acknowledgement. This does not pause inference or replace
the final stop manifest/runtime gates. Delivery failures preserve VM originals
and expose classified recovery state.

`session.restart` accepts `output_mode: pc|drive` to change destinations in the
same VM. The previous outputs and runtime must be verified first. Remote mode
commit is recorded separately from readiness; lost replies keep a transition
for exact-session recovery. Health confirms the actual destination, and
reconnect starts the existing allocation if the restart was incomplete. The
account's preferred destination changes only after readiness succeeds.

`history.export` returns all retained receipts, up to 2,000, with optional
account/session/operation filters in one consistent SQLite read transaction.
The desktop shell saves the UTF-8 JSON through its native save dialog.

`legacy.list` and `legacy.import` address only the current owned account's
mounted legacy model/workflow directories. The controller validates kind,
category, relative path and optional destination basename, then sends only
these fields to the companion. This explicit file copy is separate from the
private credential importer; original runtime images and arbitrary source
roots are excluded. Cloud/file actions remain disabled in benchmark mode.

An allocation that failed before installation/services can still be stopped
without reinstalling. `session.stop` recovers its exact notebook and SSH
fingerprint, then invokes the reviewed `stop_unprepared` remote gate. The gate
requires the same identity, no installed record, no service records/listeners
and no outputs in any new product output root. Its proof says `installed:false`
and `runtime_required:false`; it does not claim an unverified artifact was
checked. Existing Drive artifacts are untouched. Prepared or late service
failures always use the ordinary output/runtime gates.

Validate with `python -m unittest discover -s controller/tests -v`. Live Google
auth, Drive propagation, SSH and the complete cold-install budget require a new
VM trial. Unit/mocked checks cannot establish the 180-second startup target.
