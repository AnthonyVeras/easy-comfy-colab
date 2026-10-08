# Desktop release comparison

Measured on 2026-10-08 on the same Windows computer (16 GiB RAM, eight logical CPUs). Both release wrappers use the same frontend and packaged native controller. Each run uses a separate empty data directory, zero accounts and a controller mode that blocks cloud operations and credential import.

Readiness requires the rendered UI and the first native snapshot. After readiness, settle for 60 seconds and collect 600 one-second samples. Count the entire discovered process tree, including controller, WebView/browser and helper processes. CPU is divided by all eight logical CPUs; it is total machine capacity, not a single-core percentage. Process creation during collection is included.

| Metric | Tauri | Electron |
| --- | ---: | ---: |
| Interactive opening, seconds | 2.636 | 1.123 |
| Mean private memory, MiB | 196.915 | 195.090 |
| Peak private memory, MiB | 207.652 | 216.512 |
| Mean summed working set, MiB | 465.332 | 394.822 |
| Mean machine CPU, % | 0.116 | 0.041 |
| Effective collection duration, seconds | 609.543 | 608.878 |
| Installer size, MiB | 42.58 | 149.07 |
| Local budget | Pass | Pass |

The decision budget is peak private memory <=250 MiB, mean machine CPU <=1% and opening <=3 seconds. Among passing candidates choose lower private memory; a difference below 10% favors Tauri. If both fail, repair before choosing.

Private memory and working set measure different things. The summed working set can count shared DLL/browser pages more than once; it is recorded for transparency and is not the decision's private-memory metric. These idle runs do not establish memory consumption during downloads, active sessions or browser-based ComfyUI use.

Tauri's measured tree includes `easy-comfy-colab-2.exe`, `easy-comfy-controller.exe`, `msedgewebview2.exe` and `conhost.exe`. Electron includes its app subprocesses, controller and `conhost.exe`. The final machine-local receipts are excluded from Git because they contain local executable paths. Interrupted earlier runs were discarded. Electron ran after the Tauri tree was closed; no build, test suite or real application launch ran concurrently with collection.

Both candidates pass. Mean private memory differs by less than 1%; peak private memory differs by about 4.1%. Under the agreed <10% rule, Tauri is the default for this preview. Electron opens faster and uses less idle CPU; both remain within the budgets. The unsigned Tauri NSIS installer completed with exit code 0, installed outside the checkout, and the native UI loaded the existing protected account data.

Installer provenance: the measured unbundled Tauri executable SHA-256 is `7c2f4d0d00449ef01728d8bac3458484505fac54978809ba6170ff128605e6f9`; the installed executable is `800552ea547c723a90b11502f025d414c49aef7cc7cbe8c949235b9e150bea9e`. Exactly three bytes differ: Tauri's unique bundle marker changes from `__TAURI_BUNDLE_TYPE_VAR_UNK` to `__TAURI_BUNDLE_TYPE_VAR_NSS`. Normalizing only that marker makes all 5,520,384 bytes identical. The pinned upstream `tauri-utils` 2.10.1 `src/platform.rs` documents this binary patch and the NSIS marker. The installed controller and reviewed remote modules match their source SHA-256 values.

The chosen installer SHA-256 is `464b014d7bcb4733b8f361668b520d67cfe005835d8313e6174ccba2ba416223`. A desktop shortcut points to the installed executable; the legacy shortcuts and checkout were preserved.

Both local installers are unsigned. This is a development preview; clean-Windows installation, Narrator/200%, remote artifact restoration, three fresh <=180-second starts and public OAuth remain release gates.
