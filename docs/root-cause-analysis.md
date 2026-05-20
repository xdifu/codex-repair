# Codex Desktop Crash After Update — Root Cause Analysis

**Date of incident:** 2026-05-21
**Affected versions:** Codex Desktop ≥ `0.131.0` (any user upgrading from `0.130.x`)
**OS:** Windows 10/11, MSIX install, backend running as Linux ELF inside WSL2
**Symptoms at startup:**

```
Codex cannot access its local database
  Location: /mnt/c/Users/<user>/.codex/state_5.sqlite
  Cause: failed to initialize state runtime at /mnt/c/Users/<user>/.codex:
         migration 1 was previously applied but has been modified
```

After the first fix, a follow-on symptom emerged:

```
timed out waiting for state db backfill at /mnt/c/Users/<user>/.codex
  after 30s (status: running)
```

## Executive summary

The crash is caused by **two independent OpenAI-side bugs** that compound:

1. **Violation of the sqlx immutable-migration contract.**
   Between Codex `0.130.x` and `0.131.x`, OpenAI modified the *SQL content* of
   migrations 1 and 2 in `logs_2.sqlite` (which create and then evolve the
   `logs` table). sqlx hashes every migration's SQL bytes with SHA-384 at build
   time, stores the hash in the binary, and **refuses to open a database whose
   `_sqlx_migrations` table contains a stored hash that no longer matches the
   binary's expectation** — even when the actual table schema is fully
   forward-compatible. This is a hard failure with no env-var/CLI escape hatch.

2. **Hard-coded 30-second backfill timeout on GUI startup.**
   Codex Desktop blocks GUI startup until `backfill_state.status = 'complete'`.
   The backfill scans every `.jsonl` under `sessions/` and `archived_sessions/`
   to populate the `threads` metadata table. For users with large session
   histories (e.g. 3.5 GB / 325+ jsonl files), this routinely takes longer
   than 30 s. When the deadline fires, the backend exits before the lease
   times out (the production lease is **900 s**, so this 30 s GUI cap is
   inconsistent with the backend's own design).

Neither bug is local to any one user's machine. The first reproduces on every
0.130 → 0.131 upgrade with non-empty `logs_2.sqlite`; the second reproduces on
any install with enough sessions to overrun 30 s.

## Architecture context

- Codex Desktop on Windows ships as an **MSIX package** (`OpenAI.Codex_…`).
- Because of MSIX AppData virtualization, the writable `bin/` directory is
  redirected to `%LOCALAPPDATA%\Packages\<PFN>\LocalCache\Local\OpenAI\Codex\bin\`,
  but data lives at `%USERPROFILE%\.codex\` (which the package has
  `BroadFileSystemAccess` on).
- The **backend** for the Windows app is actually a **Linux ELF binary** run
  inside WSL2 (`%USERPROFILE%\.codex\bin\wsl\<hash>\codex`). The crash error
  path `/mnt/c/...` is the WSL view of the Windows drive — this is what
  initially confused which binary needed updating.
- Two SQLite databases under `%USERPROFILE%\.codex\`:
  - `state_5.sqlite` — thread metadata, backfill state, stage1 outputs,
    agent jobs, etc. (32 migrations as of `0.131.0`)
  - `logs_2.sqlite` — log records for diagnostics (2 migrations as of
    `0.131.0`; was split off from the main state DB in [#13772](https://github.com/openai/codex/issues/13772))
- Both databases use **sqlx** for migrations (Rust crate), which records the
  applied set in an internal `_sqlx_migrations` table with this schema:

  ```
  version       INTEGER PK
  description   TEXT
  installed_on  TIMESTAMP
  success       BOOLEAN
  checksum      BLOB (48 bytes = SHA-384 of the migration SQL)
  execution_time INTEGER
  ```

## Bug 1: migration checksum drift

### How sqlx normally works

sqlx requires every migration to be **immutable** once it's been released to
users. The standard guidance (in sqlx docs and Rust dev community at large):

> Once a migration has been deployed and may have been applied to any production
> database, you must never modify its SQL. Add a new migration instead.

The reason is exactly this scenario: sqlx hashes the SQL at compile time, stores
the hash in the binary, and refuses to open any DB whose stored hashes don't
match — to protect against silent schema drift between binary and DB.

### What OpenAI did

In the upgrade from `0.130.x` to `0.131.x`, the SQL inside
`codex-rs/state/migrations/logs/001_initial.sql` (and likely `002_…`) was edited
**in place** rather than added as new migrations 3, 4, etc. The new SQL produced
different SHA-384 hashes:

| Migration | DB-stored (from 0.130 install) | Binary-embedded (0.131) |
|-----------|--------------------------------|--------------------------|
| logs_2 m1 `logs` | `F477E605…` | `009639EA…` |
| logs_2 m2 `logs feedback log body` | `5C82B1A6…` | `CF6C93AF…` |

Both sides know the migration's *final* schema is identical (the `logs` table
has the same columns either way), but sqlx checks the *SQL hash*, not the
schema. So startup fails with `migration 1 was previously applied but has been
modified`.

### Why state_5.sqlite was untouched

We verified all 32 `state_5.sqlite` migration hashes already match the binary
— OpenAI did NOT modify those between versions. Only `logs_2.sqlite` was the
victim. (This was confirmed by extracting all 33 embedded checksums from the
binary using SHA-384 anchor scanning and matching them against
`_sqlx_migrations` rows.)

### Why the GUI still showed "migration 1 of state_5.sqlite" in its error

Codex's user-facing error message displays the **first** failing migration even
when the actual failure is on a downstream DB. After applying any partial repair,
the error rotated through several migrations as the backend reached different
init steps. This made the error noisy and misleading.

### The fix

For each drifted row, we verified the **actual SQLite schema is at or beyond
the post-migration state** (using `PRAGMA table_info(logs)` and confirming all
12 expected columns including `feedback_log_body`, `thread_id`, `process_uuid`,
`estimated_bytes`). Once schema-compatibility is proven, it is safe to update
just the `checksum` column in `_sqlx_migrations` to the binary's expected
value. No SQL is replayed.

The new `codex-repair.py fix-checksums` automates this end-to-end:

1. Extract every migration's `(SQL, SHA-384)` anchor from the running binary.
2. Match anchors to DB rows by checksum equality, then by description proximity,
   then by version-ordered fallback.
3. For each mismatch, run `expected_columns_from_sql()` on the binary's SQL,
   then check the live table has all those columns.
4. If safe, `UPDATE _sqlx_migrations SET checksum = ?` in a transaction.
5. Re-verify and report.

## Bug 2: 30-second backfill timeout

### Backfill in `0.131`

`state_5.sqlite` includes a `backfill_state` table:

```
id              INTEGER PK
status          TEXT    -- 'pending' | 'running' | 'complete'
last_watermark  TEXT
last_success_at INTEGER
updated_at      INTEGER
```

On startup, the backend:

1. Claims the backfill lease (configurable; production = 900 s, see [PR #11377](https://github.com/openai/codex/pull/11377)).
2. Scans `sessions/**/*.jsonl` + `archived_sessions/**/*.jsonl`.
3. For each unindexed file, reads the first line (`session_meta`), the first
   user message in the body, and `INSERT`s a row into `threads`.
4. Periodically `checkpoint_backfill(watermark)` to make progress resumable.
5. On completion, `UPDATE backfill_state SET status='complete'`.

The GUI, meanwhile, has its own startup gate: it waits for
`backfill_state.status = 'complete'` with a **hard-coded 30-second timeout**
embedded in the binary string `"timed out waiting for state db backfill at {} after {}s (status: {})"`. We searched the binary exhaustively and found no
configuration knob, environment variable, or CLI flag controlling that 30 s.

### Why this fails for some users

For a fresh install with no sessions, backfill is instant. For someone with a
year of history (here: 3.5 GB across 325 sessions + 657 MB across 40 archived
sessions), one cold scan takes >> 30 s. The backend has plenty of time (its own
lease is 900 s and it correctly resumes from `last_watermark` on restart), but
the GUI gives up at 30 s and reports the timeout, making it look like a crash.

### The fix

`codex-repair.py manual-backfill` reproduces what the backend does, but ahead
of time and from Python so we control the deadline:

1. `PRAGMA table_info(threads)` to discover the live schema dynamically — no
   hard-coded 26-column INSERT.
2. For each `.jsonl` in `sessions/` and `archived_sessions/` not already in
   `threads`:
   a. Read the first line, parse as `session_meta`.
   b. Extract `id`, `source` (handling `dict`-valued source observed in
      sub-agent rollouts, e.g. `{'subagent': {'other': 'guardian'}}`),
      `cwd`, `model_provider`, `cli_version`, etc.
   c. Scan up to 200 more lines for the first user message to populate
      `title`, `preview`, `first_user_message`.
   d. `INSERT OR IGNORE` into `threads` (idempotent).
3. `UPDATE backfill_state SET status='complete', last_watermark=<newest_path>`.

On next launch, the GUI's 30-second wait finds `status='complete'` immediately
and never times out, because the table is already fully populated.

## Investigation timeline (abridged)

The full archeology required 22 throwaway scripts; here's the dependency graph
of discoveries:

```
  ┌─ Step 1: localize the crashing binary ───────────────────────────────┐
  │  ▸ Initial assumption: codex.exe at AppData\Local\OpenAI\Codex\bin\  │
  │  ▸ After two failed sync attempts: realized MSIX AppData virtualizes│
  │    that path to LocalCache\Local\OpenAI\Codex\bin\                  │
  │  ▸ After third failed attempt: realized backend is Linux ELF in WSL,│
  │    not Windows PE. Discovered actual path via WSL process spy:      │
  │    .codex\bin\wsl\7945a00f33bdc140\codex                            │
  └──────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
  ┌─ Step 2: hypothesize migration mismatch ───────────────────────────┐
  │  ▸ Read state_5.sqlite._sqlx_migrations: 32 rows, all checksums    │
  │  ▸ Extract migration 1 SQL bytes + SHA-384 from binary:            │
  │    627EF191… matches DB exactly ⟹ state_5 m1 is NOT the problem    │
  │  ▸ Repeat for logs_2.sqlite m1/m2:                                 │
  │    F477E605… (DB) ≠ 009639EA… (binary). FOUND IT.                  │
  └─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
  ┌─ Step 3: confirm safety of checksum patch ─────────────────────────┐
  │  ▸ Diff binary's new logs m1 SQL vs the schema actually present:   │
  │    all 12 columns of logs table match. Schema is already at the    │
  │    post-migration state. Safe to patch checksum without replaying. │
  └─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
  ┌─ Step 4: apply the checksum fix ───────────────────────────────────┐
  │  ▸ Back up logs_2.sqlite + state_5.sqlite                          │
  │  ▸ UPDATE _sqlx_migrations SET checksum=… WHERE version IN (1,2)   │
  │  ▸ Codex starts further but now reports a different migration row  │
  │    fails. Iterate — total of N migrations across both DBs needed   │
  │    patching. Final count: only logs m1/m2 in this user's install.  │
  └─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
  ┌─ Step 5: backfill timeout discovered ──────────────────────────────┐
  │  ▸ Codex now opens login screen, but after sign-in errors with     │
  │    "timed out waiting for state db backfill … after 30s".          │
  │  ▸ Inspect backfill_state: status='running', threads count=316     │
  │    but 49 jsonl files unindexed.                                   │
  │  ▸ Search binary for "30s" timeout config: hard-coded; no env var. │
  │  ▸ Manual INSERT-from-jsonl pass to bring threads to 365 and       │
  │    flip status='complete'. One file's source field was a dict      │
  │    instead of str — fixed normalization, re-ran. All 365 indexed.  │
  └─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
  Codex starts in ~3 s, sidebar shows full history, no errors.
```

## Why this won't be solved by upgrading to `0.132.0`

[`rust-v0.132.0`](https://github.com/openai/codex/releases/tag/rust-v0.132.0)
(released 2026-05-20, current latest) changelog lists 24 bug fixes. None of
them touch the sqlx migration validation path or the 30 s GUI backfill cap.
Specifically the Bug Fixes section covers goal continuations, session picker
UX, multi-session MCP replay, remote sessions, Windows installer probes, and
TUI polish. **Neither bug above is fixed in 0.132.0.**

The closest upstream work is [PR #16924](https://github.com/openai/codex/pull/16924)
("fix(sqlite): don't hard fail migrator if DB is newer") which was merged
2026-04-06 and ships in `0.131+`. That PR relaxes the migrator only when the
DB knows about migrations the binary doesn't (DB is ahead). It does **not** help
the reverse case where the binary knows a different SQL for an existing migration
than what's recorded in the DB — which is exactly Bug 1.

## Suggested upstream changes

See [`upstream-bug-report.md`](upstream-bug-report.md) for a paste-ready
issue + PR description. In summary:

- **Best fix:** OpenAI internal discipline — never modify a published
  migration. Add `003_…` instead of editing `001_…`.
- **Defensive fix:** When the migrator detects a checksum mismatch, run a
  schema diff against the binary's expected post-migration state. If schemas
  are identical, log a warning and auto-update the stored checksum instead
  of hard-failing.
- **Escape hatch:** Accept an env var `CODEX_TOLERATE_MODIFIED_MIGRATIONS=1`
  (or CLI flag) that triggers the defensive-fix path explicitly.
- **Backfill timeout:** Either remove the 30 s GUI cap entirely, raise it to
  the same 900 s the backend lease uses, or expose it via config.

## Lessons (for the toolkit's future)

- **Binary identity matters.** When a Codex update fails, the very first thing
  to verify is "which binary is actually running" — MSIX virtualization and
  WSL ELF execution mean the path can be surprising.
- **sqlx migration mismatches present as opaque "DB is damaged" prompts.** The
  GUI's `[y/N] repair now?` prompt is dangerous if accepted (it will
  destructively reset state). Always refuse that prompt and investigate.
- **Backups are cheap.** Throughout the live investigation we made 12+
  backups of `state_5.sqlite` and `logs_2.sqlite` at different points. The
  total disk cost was < 100 MB and saved us at every iteration.
- **A single self-contained Python script beats 22 shell helpers.** The
  archive folder is a museum of "single-purpose snippets that worked", but
  reusability needs structure.
