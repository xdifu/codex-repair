# Upstream bug report — paste-ready for openai/codex

This file is a paste-ready issue body and PR description for filing a
comprehensive upstream report on the two bugs handled by this toolkit.

The full technical analysis backing these claims is in
[`root-cause-analysis.md`](root-cause-analysis.md). Adapt the wording / contact
info before posting.

---

## Issue: Title

```
Codex Desktop crashes after auto-update: logs_2.sqlite migration SQL was modified in place (sqlx checksum drift), and the 30s GUI backfill cap is incompatible with the 900s backend lease
```

## Issue: Body

> **Repository:** `openai/codex`
> **Labels:** `bug`, `app`, `windows-os`, `sqlite`

### Affected versions

- Crash reproduces on every install upgrading from `0.130.0-alpha.5`
  (last known good) to any `0.131.x`.
- Verified on Windows 10/11 with the MSIX `OpenAI.Codex` package and the
  bundled WSL2 Linux ELF backend (`%USERPROFILE%\.codex\bin\wsl\<hash>\codex`).
- `0.132.0` (latest as of 2026-05-20) **does not** fix either symptom.

### Symptom A (always fires first)

On launch, the GUI reports:

```
Codex cannot access its local database
  Location: /mnt/c/Users/<user>/.codex/state_5.sqlite
  Cause: failed to initialize state runtime at /mnt/c/Users/<user>/.codex:
         migration 1 was previously applied but has been modified
```

Accepting the offered "repair now" prompt is destructive (it nukes thread
metadata). Declining leaves Codex unusable.

### Symptom B (fires after symptom A is mitigated)

```
timed out waiting for state db backfill at /mnt/c/Users/<user>/.codex
  after 30s (status: running)
```

### Root causes (verified by binary archaeology)

**A. Modified migrations in `logs_2.sqlite`.**

The SQL bytes of migration 1 (`logs`) and migration 2 (`logs feedback log
body`) were edited in place between `0.130.x` and `0.131.x`. sqlx stores the
SHA-384 of each migration's SQL at build time, and refuses any DB whose
stored checksum doesn't match — even though the resulting *table schema* is
identical between the old and new migration SQL.

Concrete evidence from a real `0.130 → 0.131` upgrade:

| Migration | DB-stored hash (post-0.130) | Binary-embedded hash (0.131) |
|-----------|------------------------------|------------------------------|
| `logs_2` m1 `logs`                | `F477E605…` | `009639EA…` |
| `logs_2` m2 `logs feedback log body` | `5C82B1A6…` | `CF6C93AF…` |

All 32 `state_5.sqlite` migration checksums *did* match between versions, so
this is isolated to `logs_2.sqlite`. We extracted both SQL bodies by SHA-384
anchor scanning of the backend ELF and confirmed the schemas they produce are
fully forward-compatible — the difference is in the SQL bytes themselves
(formatting, comments, possibly minor reordering of constraints), not in any
schema-meaningful change. So in this case sqlx is hard-failing on a purely
cosmetic SQL diff.

This violates the sqlx documented contract that published migrations are
immutable. From sqlx-cli docs:

> Migrations are read-only once they have been added to the project. To make
> changes, add a new migration file rather than modifying an existing one.

**B. Hard-coded 30-second GUI backfill timeout.**

`state_5.sqlite.backfill_state.status` must reach `'complete'` before the GUI
unblocks startup. The GUI cap is hard-coded as `30s` (string
`"timed out waiting for state db backfill at {} after {}s (status: {})"`)
with no environment variable, config knob, or CLI flag to override it. The
backend's own backfill lease is **900 s** (PR #11377). For users with a few
hundred MB of session jsonl, a cold backfill routinely takes 30–120 s, so the
GUI gives up before the backend completes.

### Reproduction

1. Install Codex Desktop `0.130.0-alpha.5` (or any 0.130 release). Use it
   normally for ≥ 1 day so `logs_2.sqlite` accumulates rows and
   `_sqlx_migrations` is populated.
2. Let the app auto-update to `0.131.x`.
3. Launch — receive symptom A immediately.
4. After patching `_sqlx_migrations.checksum` to the binary-expected values
   for `logs_2` m1/m2 (or running this toolkit's `fix-checksums --apply`),
   relaunch. With enough sessions on disk (> ~50 MB total), receive symptom B.

### Proposed fixes

**Fix 1 (preferred): never modify a published migration.**
In `codex-rs/state/migrations/logs/`, revert m1.sql and m2.sql to their
0.130-era bytes, and express the new desired schema as a m3.sql / m4.sql.
This is the sqlx-canonical approach and avoids any client-side compatibility
shim.

**Fix 2 (defensive): forgive cosmetic SQL drift.**
In `codex-rs/state/src/runtime.rs` (post-PR #16924 path), when the
`MigrateDatabase::migrate` call returns `MigrationError::VersionMismatch`,
catch it and:

1. Resolve the binary's expected SQL for that migration.
2. Diff the live SQLite schema against the schema the new SQL would produce
   (column names, types, NOT NULL, indexes). The toolkit shows how to do this
   purely from `PRAGMA table_info` + a lightweight SQL parser — no need to
   actually replay.
3. If they're already identical, log a warning and
   `UPDATE _sqlx_migrations SET checksum = ?` to the binary's hash.
4. If they differ, fall back to today's hard-fail behavior.

This is symmetric to PR #16924 (which forgives the *opposite* direction: DB
newer than binary).

**Fix 3 (escape hatch, minimum-effort):**
Accept `CODEX_TOLERATE_MODIFIED_MIGRATIONS=1` as an explicit opt-in
environment variable that triggers Fix 2's path. Useful when in-house testing
is the source of the drift.

**Fix 4 (backfill cap):**
- Remove the 30 s GUI cap entirely; show a spinner with progress until
  `backfill_state.status='complete'` or the backend lease (900 s) expires.
- Or expose the cap via `~/.codex/config.toml` (e.g.
  `[startup] backfill_timeout_secs = 30`).
- Either change makes the GUI consistent with the backend's own design.

### Why 0.132.0 doesn't help

[`rust-v0.132.0` changelog](https://github.com/openai/codex/releases/tag/rust-v0.132.0)
lists 24 bug fixes (goal continuations, session picker UX, MCP replay,
remote sessions, Windows installer probes, TUI polish). None of them touches
the migrator validation path or the GUI's startup timeout. We grep'd the
0.132.0 changelog and there are no mentions of "migration", "sqlx",
"backfill timeout", "_sqlx_migrations", or "state db".

### Related issues / PRs

- #23251 — `WSL CLI cannot share Windows Codex App CODEX_HOME: migration 1
  was previously applied but has been modified` (open)
- #17304 — `Desktop project sidebar hides active threads after state DB
  migration drift` (open; family of related drift bugs)
- #17354, #17540, #18364, #19873 — overlapping sidebar / thread-disappearing
  reports stemming from `_sqlx_migrations` drift after updates
- #16924 — `fix(sqlite): don't hard fail migrator if DB is newer` (merged;
  fixes the opposite direction)
- #11377 — `feat: prevent double backfill` (introduced the 900 s lease)
- #16877 — `Make thread metadata updates tolerate pending backfill` (open)
- #13772 — `Move sqlite logs to a dedicated database` (context for why
  `logs_2.sqlite` exists)

### How users can recover today (without an upstream fix)

A standalone Python toolkit is available at the user's repo. It:

1. Auto-locates the active backend binary.
2. Extracts every embedded migration checksum by scanning the ELF for
   `(sql, sha384(sql))` anchors.
3. Diffs against each DB's `_sqlx_migrations`.
4. **Verifies schema compatibility** before rewriting any checksum.
5. Reproduces backfill in Python (independent of the 30 s GUI cap), then
   marks `backfill_state.status='complete'`.

Source code and full analysis are at the linked toolkit. We are happy to
contribute the schema-diff helper or a `--tolerate-modified-migrations`
runtime flag as a PR upstream if maintainers prefer that approach over
canonical sqlx hygiene.

---

## PR draft: Title

```
state: tolerate cosmetic migration SQL drift when post-migration schema matches
```

## PR draft: Description

```markdown
## Motivation

Today, sqlx fails Codex startup with `migration N was previously applied but
has been modified` whenever the SQL bytes of a published migration are
edited in place. PR #16924 already relaxed the opposite direction (DB newer
than binary); this PR is the symmetric counterpart.

Concretely, the 0.130 → 0.131 upgrade modified `logs_2.sqlite` migrations 1
and 2 in place. Every user upgrading hit:

    migration 1 was previously applied but has been modified

even though the post-migration schema is identical. This blocks Codex from
opening at all (issue #23251, #17304, #17354, #17540, #18364, #19873).

## Approach

In the migrator's `VersionMismatch` arm:

1. Look up the migration's `description` and its current expected SQL from
   the build-embedded set.
2. Parse the expected SQL's `CREATE TABLE` / `ALTER TABLE` / `ADD COLUMN`
   targets to derive `expected_columns`.
3. Run `PRAGMA table_info(<table>)` on the live DB.
4. If every column in `expected_columns` is present in the live schema,
   `UPDATE _sqlx_migrations SET checksum = <new_hash>` and log a warning.
5. Otherwise fall back to today's hard-fail.

Behind a feature flag `tolerate_drift` on the runtime config; default off in
release builds for the immediate future, but easy to enable per-install via
env (`CODEX_TOLERATE_MODIFIED_MIGRATIONS=1`).

## Why not just enforce immutability?

That is the canonical answer (see sqlx-cli docs) and we should still do it
internally — this PR is the user-side belt to complement the
already-existing suspenders. Even with perfect process discipline going
forward, every Codex install on the planet that came up through the 0.130
→ 0.131 corridor is currently broken and needs *some* form of recovery
path. This PR is that path.

## Tests

- `migrator::tests::tolerate_drift_when_schema_matches`
- `migrator::tests::reject_drift_when_schema_diverges`
- `migrator::tests::env_var_only_unblocks_when_set`
- End-to-end: open a synthetic `logs_2.sqlite` whose `_sqlx_migrations` has
  the 0.130-era checksums, verify startup succeeds and a warning is logged.
```

---

## How to actually file this

1. Fork [`openai/codex`](https://github.com/openai/codex).
2. Open a new issue under that repo, copy the **Issue: Title** + **Issue: Body**
   sections above, adjust username/links, attach the `codex-repair.py
   extract-checksums` JSON output for your binary as evidence.
3. (Optional, for the PR) Create a branch like `fix-tolerate-migration-drift`,
   implement the migrator change in `codex-rs/state/src/runtime.rs`, push,
   and open a PR with the **PR draft** section as the description.
4. Cross-link the new issue/PR to related ones (#23251, #17304) so reviewers
   see the family.
