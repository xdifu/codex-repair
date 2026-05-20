# archive/ — historical investigation scripts

This directory contains the 22 single-purpose scripts that were written
**during** the live 2026-05-21 repair session, before they were superseded
by the unified [`../codex-repair.py`](../codex-repair.py).

**Do not use these for new incidents.** They are kept only for forensic
reference. Every capability they had is now in `codex-repair.py` with
proper safety checks, dynamic schema discovery, dry-run mode, and
isolated-copy mode. Several of them also contain hard-coded constants
(specific binary paths, specific 0.131-era checksums) that won't match
future Codex versions.

## Index of what each script did

| Script | Purpose | Superseded by (in codex-repair.py) |
|--------|---------|-----------------------------------|
| `codex-binary-analyze.py` | Locate SQL strings inside the backend ELF | `scan_binary_anchors()` |
| `codex-extract-migration.py` | Hand-extract migration 1 SQL + SHA-384 to validate the hashing approach | `scan_binary_anchors()` |
| `codex-precise-extract.py` | Same but with tighter offsets | (redundant) |
| `codex-compare-old-new.py` | Diff migration 1 SQL between old (0.130) and new (0.131) binaries to confirm OpenAI modified it | (one-shot; no longer needed) |
| `codex-find-logs-migration.py` | Pinpoint the logs_2.sqlite migration block in the binary | `_locate_migration_cluster()` |
| `codex-find-all-expected.py` | First implementation of "extract every migration's expected checksum" | `cmd_extract_checksums` |
| `codex-byte-search-checksums.py` | Reverse search: take DB-stored checksums and look for them in the binary as confirmation | (redundant with anchor matching) |
| `codex-verify-logs-schema.py` | Validate that the user's `logs_2.sqlite` schema matches what the new migration's SQL would produce | `check_schema_compat()` |
| `codex-fix-logs2-checksum.py` | Apply the checksum fix (hard-coded values for 0.131) | `cmd_fix_checksums` (with auto-extracted checksums) |
| `codex-analyze-backfill.py` | Report `threads` count, `backfill_state`, unindexed file count | `collect_backfill_status()` |
| `codex-find-cli-subcommands.py` | Inspect binary's argparse subcommands (looking for an undocumented "skip backfill" option) | (one-shot reconnaissance) |
| `codex-find-timeout-config.py` | Search binary for env vars / config knobs controlling the 30 s backfill timeout (none found) | (one-shot reconnaissance) |
| `codex-find-timeout-source.py` | Locate the literal `"after 30s"` string in the binary | (one-shot reconnaissance) |
| `codex-find-unindexed.py` | Enumerate jsonl files not yet in `threads` | (inlined in `cmd_manual_backfill`) |
| `codex-manual-backfill.py` | Manually INSERT thread metadata from unindexed jsonl + flip `backfill_state.status` | `cmd_manual_backfill` (with dynamic schema discovery) |
| `codex-wsl-spy.sh` | Watch WSL processes to find which binary was actually being executed by the GUI | (one-shot reconnaissance — superseded by `find_backend_binary()`) |
| `codex-bf-runner.sh` | Attempt to invoke the backfill backend standalone | (one-shot reconnaissance — failed; no public subcommand) |
| `codex-bf-status.sh` | Check backfill_state from WSL side | (redundant) |
| `codex-check-current.sh` | Snapshot current state from WSL side | (redundant) |
| `codex-fix.ps1` | Early **wrong** repair attempt: tried to sync Windows codex.exe binaries (misunderstood that the backend is in WSL) | DEPRECATED |
| `codex-fix-v2.ps1` | Second **wrong** repair attempt with the same mistake | DEPRECATED |
| `codex-spy.ps1` | First Windows process spy (later replaced by `codex-wsl-spy.sh` once we realized the real backend is in WSL) | DEPRECATED |
