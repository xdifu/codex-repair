# Codex Repair Toolkit

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)
[![Upstream issue](https://img.shields.io/badge/upstream-openai%2Fcodex%2323251-red.svg)](https://github.com/openai/codex/issues/23251)

> Maintained by [**@xdifu**](https://github.com/xdifu). Contributions and bug
> reports welcome via Issues.

Unified repair tool for **Codex Desktop on Windows + WSL backend**.

Handles the two crash modes observed when Codex Desktop's auto-update jumps from
`0.130.x` to `0.131.x` (or any future similar upgrade where OpenAI breaks the
sqlx "immutable migration" contract):

1. **Migration checksum drift** — startup fails with
   `migration X was previously applied but has been modified`.
2. **Backfill timeout** — Codex GUI opens but errors with
   `timed out waiting for state db backfill ... after 30s (status: running)`.

See [`docs/root-cause-analysis.md`](docs/root-cause-analysis.md) for the full technical writeup of
how these were discovered and why they happen.

## Quick start

```powershell
# Interactive diagnose + repair (recommended; prompts before each step):
.\repair.ps1

# Just diagnose, never touch the DB:
.\repair.ps1 -Mode doctor

# Zero-risk dry-run against a temp copy of the DBs (safe to run while Codex is open):
.\repair.ps1 -Mode fix -Isolated

# Apply the fix (will offer to stop Codex first):
.\repair.ps1 -Mode fix -Apply
```

Or call the Python script directly:

```bash
python codex-repair.py doctor                          # diagnose
python codex-repair.py doctor --use-isolated-copy      # diagnose, zero contact with live DB
python codex-repair.py fix                             # auto-detect & dry-run
python codex-repair.py fix --apply                     # actually repair
python codex-repair.py extract-checksums               # dump binary's expected checksums
python codex-repair.py -h                              # full help
```

## Directory layout

```
.codex-repair\
├── codex-repair.py     ← the only script you need; everything is here
├── repair.ps1          ← Windows-friendly wrapper (interactive prompts)
├── README.md           ← this file
├── docs\
│   ├── root-cause-analysis.md    ← deep technical writeup
│   └── upstream-bug-report.md    ← ready-to-paste GitHub issue / PR description
└── archive\            ← original 22 ad-hoc scripts from the 2026-05-21 incident
                          (kept for historical reference; NOT for re-use)
```

## What `codex-repair.py` does

### Subcommands

| Subcommand | What it does | Mutates DB? |
|------------|--------------|-------------|
| `doctor`   | Read-only diagnosis: locates the backend binary, scans it for embedded migration checksums, compares against each DB's `_sqlx_migrations`, and checks `backfill_state` + unindexed jsonl files. Reports a status code. | No |
| `fix`      | Run `doctor` first; based on what it finds, calls `fix-checksums` and/or `manual-backfill`. Default is dry-run; pass `--apply` to actually mutate. | Only with `--apply` |
| `fix-checksums` | For each DB migration whose stored checksum doesn't match the binary's hash of the same migration's SQL, **verify the actual table schema is already at the post-migration state**, then rewrite the stored checksum. Will refuse to touch a row whose schema is NOT compatible. | Only with `--apply` |
| `manual-backfill` | Discover sessions `jsonl` files that aren't yet in `threads`, insert thread metadata for each (dynamically against the current schema), and mark `backfill_state.status='complete'`. | Only with `--apply` |
| `extract-checksums` | Dump every expected migration checksum found in the backend binary. Useful for debugging or sharing in a GitHub issue. | No |

### Global flags

| Flag | Meaning |
|------|---------|
| `--codex-home PATH` | Codex home dir. Default `%USERPROFILE%\.codex`. |
| `--binary PATH`     | Backend binary. Default: auto-detect newest in `{codex-home}\bin\wsl\*\codex` (falls back to `bin\codex.exe`). |
| `--apply`           | Actually mutate the DB. Without this, every subcommand runs dry-run. |
| `--use-isolated-copy` | Copy the DBs to a temp dir, then operate on copies. The live DB is never opened. Implies dry-run. **Recommended whenever Codex is running.** |
| `-v` / `--verbose`  | More output, including the binary scan region and anchor count. |

### Exit codes from `doctor`

| Code | Meaning |
|------|---------|
| `0`  | Healthy — no action needed. |
| `10` | Migration checksum drift detected. |
| `11` | Backfill stuck (unindexed files or `status != complete`). |
| `12` | Both. |
| `20` | Backend binary not found. |
| `21` | A required database file is missing. |
| `30` | User aborted. |
| `1`  | Other error. |

## Safety guarantees

1. **Default is dry-run.** Every subcommand prints what it WOULD change. Only `--apply` actually mutates.
2. **Backup before mutate.** Every write is preceded by a timestamped backup of the affected DB (and its `-wal` / `-shm` siblings) named like `state_5.sqlite.bak-fix-checksums-<timestamp>`.
3. **Schema verification.** `fix-checksums` only rewrites a checksum after confirming the actual SQLite schema already contains every column the new migration SQL expects. If the schema is older than the binary expects, it refuses and tells you to manually investigate (because the migration body really needs to run, not just have its checksum patched).
4. **Atomic transactions.** Every mutation is wrapped in `BEGIN IMMEDIATE … COMMIT` and rolled back on any error.
5. **Idempotent.** Running `fix --apply` twice in a row is a no-op the second time.
6. **`--use-isolated-copy`.** When passed, the script copies the DBs to a private temp directory and operates only on those. Your real DB is never touched, even read-only. Useful for testing while Codex is running.

## When to use this

- After a Codex Desktop auto-update, you see one of these errors:
  - `Codex couldn't start because its local database appears to be damaged. ... migration N was previously applied but has been modified`
  - `timed out waiting for state db backfill at ... after 30s (status: running)`
- You want to verify your `.codex\` state is healthy after an upgrade.
- You want to dump the backend's expected checksums for comparison with a friend's install or a GitHub bug report.

## When NOT to use this

- If the error is something **other** than the two listed above (e.g., file permission errors, corrupted jsonl, `state_5.sqlite` truly corrupt per `PRAGMA integrity_check`). This tool will not help — it only handles the sqlx-migration-drift + backfill-timeout classes.
- If `.codex\sessions\` itself is missing or empty. This tool reconstructs **metadata** from sessions; it cannot recreate session content.

## Related upstream issues

The bug being repaired here is **OpenAI's**, not your computer's. Public issues:

- [#23251](https://github.com/openai/codex/issues/23251) — `WSL CLI cannot share Windows Codex App CODEX_HOME: migration 1 was previously applied but has been modified` (Open; describes one specific repro path)
- [#17304](https://github.com/openai/codex/issues/17304) — `Desktop project sidebar hides active threads after state DB migration drift` (Open; family of related drift bugs)
- [#16924](https://github.com/openai/codex/pull/16924) — `fix(sqlite): don't hard fail migrator if DB is newer` (Merged; fixes the OTHER direction, where DB is newer than binary)
- [`docs/upstream-bug-report.md`](docs/upstream-bug-report.md) — paste-ready issue body / PR description for filing a comprehensive report.

## Requirements

- Windows 10/11 with Codex Desktop installed
- Python 3.10 or newer on PATH
- The `sqlite3` module (bundled with stock Python)

No third-party Python packages are required.

## Authorship & history

Distilled from a ~5-hour live diagnostic session on 2026-05-21 where a Codex
Desktop update from `0.130.0-alpha.5` to `0.131.0-alpha.9` triggered both bugs
above. The original 22 ad-hoc scripts that uncovered the root cause are
preserved under [`archive/`](archive/) for posterity. See
[`docs/root-cause-analysis.md`](docs/root-cause-analysis.md) for the full story.
