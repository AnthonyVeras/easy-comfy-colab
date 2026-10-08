# Security and private data

Never include refresh tokens, provider keys, private SSH keys, bearer URLs or credential files in an issue, log, diagnostic or repository. Diagnostics redact secrets and expose only the minimum state needed to recover.

Accounts are verified by their Google subject. Privileged operations execute in the native controller. The renderer has a restricted action bridge and does not hold credentials. Services and SSH tunnels listen only on loopback.

Do not stop a VM inferred only from its GPU or open HTTP port. Do not bypass pending output delivery or required runtime preservation. Unknown state requires recovery, not teardown.

Public security reporting/contact information must be supplied by the maintainer before publication; this local project does not transmit reports automatically.
