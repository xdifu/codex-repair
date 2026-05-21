#!/usr/bin/env python3
"""codex-repair.py — Unified repair tool for Codex Desktop (Windows + WSL backend).

Handles the two crash modes observed in Codex 0.130 -> 0.131 upgrade:
  1. sqlx migration checksum mismatch:
     "migration X was previously applied but has been modified"
     (OpenAI modified logs_2.sqlite migration 1+2 SQL between versions,
      violating sqlx's immutable-migration contract)
  2. backfill 30s timeout on startup:
     "timed out waiting for state db backfill ... after 30s (status: running)"
     (Backfill scans all sessions/*.jsonl; large session histories don't finish in 30s)

Subcommands:
  doctor                Diagnose only, never write. Default if no subcommand.
  fix                   Auto-detect & fix both issues. Dry-run by default.
  fix-checksums         Only fix migration checksum mismatch.
  manual-backfill       Only do manual thread metadata backfill.
  extract-checksums     Extract every expected migration checksum from binary.

Run `python codex-repair.py -h` or `python codex-repair.py <cmd> -h` for help.

Safety contract:
  * Read-only by default. `--apply` is required to mutate any database.
  * `--use-isolated-copy` copies DBs to a private temp dir before reading,
    so a running Codex is never touched. Implies dry-run.
  * Every mutation is wrapped in a transaction and preceded by a timestamped
    backup of the affected database (plus its WAL/SHM if present).
  * Schema is verified compatible before any checksum is rewritten.
  * Idempotent: re-running on an already-fixed install is a no-op.

Author note: this was distilled from ~22 ad-hoc diagnostic scripts written
during the 2026-05-21 root-cause investigation. See docs/root-cause-analysis.md
for the full archeology.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import mmap
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_VERSION = "1.0.0"

# The two state databases Codex 0.131 backend uses.
STATE_DB_NAME = "state_5.sqlite"
LOGS_DB_NAME = "logs_2.sqlite"

# Defaults used if auto-detection fails.
DEFAULT_CODEX_HOME = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".codex"

# Status exit codes for `doctor`.
EXIT_HEALTHY = 0
EXIT_CHECKSUM_DRIFT = 10
EXIT_BACKFILL_STUCK = 11
EXIT_BOTH = 12
EXIT_NO_BINARY = 20
EXIT_NO_DB = 21
EXIT_USER_ABORT = 30
EXIT_ERROR = 1

# SQL keywords sqlx migrations always start with (used as anchor candidates).
SQL_START_KEYWORDS = (
    b"CREATE TABLE",
    b"ALTER TABLE",
    b"DROP TABLE",
    b"CREATE INDEX",
    b"DROP INDEX",
    b"CREATE VIEW",
    b"DROP VIEW",
    b"UPDATE ",
    b"INSERT INTO",
    b"DELETE FROM",
    b"WITH ",
    b"PRAGMA ",
    b"BEGIN",
    b"-- ",
)

SHA384_LEN = 48
MAX_MIGRATION_SQL_LEN = 30_000  # sqlx migrations rarely exceed this


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class MigrationRow:
    """One row of `_sqlx_migrations` from a database."""

    version: int
    description: str
    installed_on: str
    checksum_hex: str


@dataclasses.dataclass
class BinaryAnchor:
    """A (sql, sha384) pair discovered in the backend binary."""

    offset: int
    sql_len: int
    checksum_hex: str
    sql_first_line: str  # for human display only
    sql_full_lower: str = ""  # full SQL bytes lowercased, for content matching

    @property
    def is_create_or_alter(self) -> bool:
        first = self.sql_first_line.upper()
        return any(first.startswith(kw.decode().strip()) for kw in SQL_START_KEYWORDS)


@dataclasses.dataclass
class ChecksumDiff:
    """A single migration row that disagrees with the binary."""

    db_path: Path
    db_row: MigrationRow
    binary_anchor: Optional[BinaryAnchor]
    schema_ok: bool
    schema_notes: list[str]


@dataclasses.dataclass
class BackfillStatus:
    """Snapshot of `backfill_state` + thread/session counts."""

    status: str
    last_watermark: Optional[str]
    last_success_at: Optional[int]
    indexed_threads: int
    sessions_jsonl_count: int
    archived_jsonl_count: int
    unindexed_files: list[Path]

    @property
    def is_stuck(self) -> bool:
        # "Stuck" means we have unindexed files AND status isn't 'complete'.
        # Even with status='complete', if there are unindexed files we should re-run.
        return bool(self.unindexed_files) or self.status != "complete"


# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------


class Console:
    """Tiny color/no-color console helper. Writes to `stream` (default stdout).

    When emitting machine-readable output to stdout (e.g. `extract-checksums
    --json`), construct with `stream=sys.stderr` so progress decoration doesn't
    pollute the data stream.
    """

    def __init__(self, verbose: bool = False, stream=None):
        self.verbose = verbose
        self.stream = stream if stream is not None else sys.stdout
        self._use_color = (
            getattr(self.stream, "isatty", lambda: False)()
            and os.environ.get("NO_COLOR") is None
        )

    def _c(self, code: str, msg: str) -> str:
        if not self._use_color:
            return msg
        return f"\x1b[{code}m{msg}\x1b[0m"

    def _p(self, msg: str = "") -> None:
        print(msg, file=self.stream)

    def header(self, msg: str) -> None:
        self._p()
        self._p(self._c("1;36", "=" * 78))
        self._p(self._c("1;36", f"  {msg}"))
        self._p(self._c("1;36", "=" * 78))

    def section(self, msg: str) -> None:
        self._p()
        self._p(self._c("1;33", f"▶ {msg}"))

    def ok(self, msg: str) -> None:
        self._p(self._c("32", f"  ✓ {msg}"))

    def warn(self, msg: str) -> None:
        self._p(self._c("33", f"  ⚠ {msg}"))

    def err(self, msg: str) -> None:
        self._p(self._c("31", f"  ✗ {msg}"))

    def info(self, msg: str) -> None:
        self._p(f"    {msg}")

    def debug(self, msg: str) -> None:
        if self.verbose:
            self._p(self._c("90", f"    [dbg] {msg}"))


con = Console()  # global; reconfigured in main()


# ---------------------------------------------------------------------------
# Auto-detection helpers
# ---------------------------------------------------------------------------


def find_backend_binary(codex_home: Path) -> Optional[Path]:
    """Return the newest WSL backend binary, or Windows fallback, or None."""
    wsl_dir = codex_home / "bin" / "wsl"
    candidates: list[tuple[float, Path]] = []
    if wsl_dir.is_dir():
        for sub in wsl_dir.iterdir():
            cand = sub / "codex"
            if cand.is_file():
                candidates.append((cand.stat().st_mtime, cand))
    win_cand = codex_home / "bin" / "codex.exe"
    if win_cand.is_file():
        candidates.append((win_cand.stat().st_mtime, win_cand))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def detect_rollout_path_scheme(state_db: Path) -> str:
    """Return '/mnt/c/' or 'C:\\' based on existing threads.rollout_path samples.

    Defaults to '/mnt/c/' (WSL Linux backend) if table is empty.
    """
    try:
        con_ro = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        cur = con_ro.cursor()
        cur.execute("SELECT rollout_path FROM threads WHERE rollout_path IS NOT NULL LIMIT 5")
        rows = cur.fetchall()
        con_ro.close()
    except sqlite3.Error:
        return "/mnt/c/"
    for (p,) in rows:
        if p and p.startswith("/mnt/"):
            return "/mnt/c/"
        if p and len(p) > 2 and p[1] == ":":
            return "windows"
    return "/mnt/c/"


def windows_path_to_rollout(win_path: Path, scheme: str) -> str:
    """Convert a Windows filesystem path to the scheme stored in threads.rollout_path."""
    p = str(win_path).replace("\\", "/")
    if scheme == "/mnt/c/":
        if len(p) > 1 and p[1] == ":":
            return "/mnt/" + p[0].lower() + p[2:]
        return p
    # 'windows' scheme: keep as-is with forward slashes
    return p


# ---------------------------------------------------------------------------
# Database access (always read-only unless explicitly required)
# ---------------------------------------------------------------------------


@contextmanager
def sqlite_ro(db: Path) -> Iterator[sqlite3.Connection]:
    """Open SQLite read-only. Safe even if Codex has the DB open in WAL mode."""
    uri = f"file:{db}?mode=ro"
    cn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        yield cn
    finally:
        cn.close()


@contextmanager
def sqlite_rw(db: Path) -> Iterator[sqlite3.Connection]:
    """Open SQLite read-write. Caller must commit/rollback.

    NOTE: This will fight Codex for the write lock if Codex is running and tries
    to write at the same instant. Callers should ask the user to close Codex first
    OR run this on a backup copy.
    """
    cn = sqlite3.connect(str(db), timeout=30)
    cn.isolation_level = None  # we'll manage txn manually with BEGIN/COMMIT
    try:
        yield cn
    finally:
        cn.close()


def read_migrations(db: Path) -> list[MigrationRow]:
    rows: list[MigrationRow] = []
    with sqlite_ro(db) as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT version, description, installed_on, hex(checksum) "
            "FROM _sqlx_migrations ORDER BY version"
        )
        for v, d, t, h in cur.fetchall():
            rows.append(
                MigrationRow(
                    version=int(v),
                    description=str(d),
                    installed_on=str(t) if t else "",
                    checksum_hex=str(h).upper(),
                )
            )
    return rows


def read_table_columns(db: Path, table: str) -> list[tuple[str, str, int]]:
    """Return [(col_name, declared_type, notnull)] for `table` via PRAGMA table_info."""
    with sqlite_ro(db) as cn:
        cur = cn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        rows = cur.fetchall()
    # PRAGMA columns: cid, name, type, notnull, dflt_value, pk
    return [(r[1], r[2], int(r[3])) for r in rows]


def read_backfill_state(db: Path) -> Optional[tuple[str, Optional[str], Optional[int]]]:
    try:
        with sqlite_ro(db) as cn:
            cur = cn.cursor()
            cur.execute("SELECT status, last_watermark, last_success_at FROM backfill_state")
            r = cur.fetchone()
            if not r:
                return None
            return (str(r[0]), r[1], r[2])
    except sqlite3.Error as exc:
        con.debug(f"read_backfill_state error: {exc}")
        return None


def read_indexed_rollout_paths(db: Path) -> set[str]:
    with sqlite_ro(db) as cn:
        cur = cn.cursor()
        cur.execute("SELECT rollout_path FROM threads WHERE rollout_path IS NOT NULL")
        return {row[0] for row in cur.fetchall()}


def read_threads_count(db: Path) -> int:
    with sqlite_ro(db) as cn:
        cur = cn.cursor()
        cur.execute("SELECT COUNT(*) FROM threads")
        return int(cur.fetchone()[0])


def _collect_all_descriptions(*dbs: Path) -> list[str]:
    """Collect every migration `description` string across all given DBs.

    Used as a binary scan locator: these strings are uniquely present in the
    embedded migration metadata, so finding them tells us where the cluster is.
    """
    out: list[str] = []
    for db in dbs:
        if not db.exists():
            continue
        try:
            for row in read_migrations(db):
                if row.description:
                    out.append(row.description)
        except sqlite3.Error:
            continue
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


# ---------------------------------------------------------------------------
# Binary scanning: extract (sql, sha384) anchors
# ---------------------------------------------------------------------------


def _locate_migration_cluster(
    mm: mmap.mmap,
    size: int,
    descriptions: Optional[list[str]] = None,
) -> Optional[tuple[int, int]]:
    """Find the migration data cluster in the backend binary.

    Strategy A (preferred): If we have a list of migration `descriptions` (read
    from the DB's `_sqlx_migrations` table), find them in the binary. The
    convex hull of their positions is the migration cluster — these strings
    only appear in the migration metadata section.

    Strategy B (fallback): Locate the densest cluster of CREATE TABLE positions
    that ALSO contains a valid sqlx anchor (SHA-384 check). This filters out
    false positives like SQLite's statically-linked internal SQL templates.

    Returns (start, end) bounds (~1 MB window centered on the cluster), or None.
    """
    # --- Strategy A: description-based localization ---
    if descriptions:
        found: list[int] = []
        for desc in descriptions:
            # Need at least 5 chars to avoid false positives with very short names.
            if len(desc) < 5:
                continue
            needle = desc.encode("utf-8")
            # Each description may appear multiple times (in error strings, etc.).
            # We collect ALL positions and let the clustering step pick the right one.
            s = 0
            while True:
                p = mm.find(needle, s)
                if p == -1:
                    break
                found.append(p)
                s = p + 1
        if found:
            found.sort()
            # Find the densest cluster of these positions: a 2 MB window
            # is more than enough for any sqlx migration set.
            window = 2_000_000
            best_count = 0
            best_lo = found[0]
            best_hi = found[0]
            n = len(found)
            j = 0
            for i in range(n):
                if j < i:
                    j = i
                while j < n and found[j] - found[i] <= window:
                    j += 1
                count = j - i
                if count > best_count:
                    best_count = count
                    best_lo = found[i]
                    best_hi = found[j - 1]
            rstart = max(0, best_lo - 500_000)
            rend = min(size, best_hi + 500_000)
            return (rstart, rend)

    # --- Strategy B: CREATE TABLE density + sample SHA-384 validation ---
    positions: list[int] = []
    s = 0
    while True:
        p = mm.find(b"CREATE TABLE", s)
        if p == -1:
            break
        positions.append(p)
        s = p + 1
    if len(positions) < 3:
        return None

    window = 3_000_000
    n = len(positions)
    candidates: list[tuple[int, int, int]] = []  # (count, lo, hi)
    j = 0
    for i in range(n):
        if j < i:
            j = i
        while j < n and positions[j] - positions[i] <= window:
            j += 1
        count = j - i
        candidates.append((count, positions[i], positions[j - 1]))
    # Try densest clusters in descending order; the first one that contains a
    # valid sqlx anchor is the real migration cluster.
    candidates.sort(reverse=True)
    for count, lo, hi in candidates[:5]:
        if _cluster_has_valid_anchor(mm, max(0, lo - 50_000), min(size, hi + 50_000)):
            return (max(0, lo - 500_000), min(size, hi + 500_000))
    return None


def _cluster_has_valid_anchor(mm: mmap.mmap, rstart: int, rend: int) -> bool:
    """Return True iff at least one (sql, sha384) anchor exists in [rstart, rend].

    This is a CHEAP probe: we sample up to 20 CREATE TABLE positions in the
    cluster and brute-force the L range. As soon as ANY anchor matches, return.
    """
    sample_starts: list[int] = []
    s = rstart
    while len(sample_starts) < 20:
        p = mm.find(b"CREATE TABLE", s, rend)
        if p == -1:
            break
        sample_starts.append(p)
        s = p + 1
    for sp in sample_starts:
        max_L = min(3000, rend - sp - SHA384_LEN)
        for L in range(40, max_L):
            if hashlib.sha384(mm[sp : sp + L]).digest() == mm[sp + L : sp + L + SHA384_LEN]:
                return True
    return False


def scan_binary_anchors(
    binary: Path,
    region_hint: Optional[tuple[int, int]] = None,
    descriptions_hint: Optional[list[str]] = None,
) -> list[BinaryAnchor]:
    """Scan a Codex backend binary for embedded (sql, sha384(sql)) migration anchors.

    sqlx stores each migration as a string + its SHA-384 checksum, adjacent in .rodata.

    Performance algorithm:
      1. Auto-locate the migration cluster, preferring description-based
         localization when DB migration descriptions are known (descriptions_hint).
         Fallback: CREATE TABLE density + SHA-384 sample validation.
      2. For each SQL_START candidate within the cluster, try end positions at
         statement boundaries (`;`, `\\n`) — instead of brute-forcing every L
         in 40..30000.

    Returns:
        List of BinaryAnchor sorted by offset.
    """
    with binary.open("rb") as f:
        size = binary.stat().st_size
        if size < 1024:
            return []
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if region_hint is None:
                hint = _locate_migration_cluster(mm, size, descriptions_hint)
                if hint is None:
                    con.debug("no migration cluster found; falling back to last 10 MB")
                    rstart = max(0, size - 10_000_000)
                    rend = size
                else:
                    rstart, rend = hint
            else:
                rstart, rend = region_hint
            con.debug(f"scanning binary {binary.name} bytes [{rstart:,}..{rend:,}]")

            # Find candidate SQL start positions.
            starts: list[int] = []
            for kw in SQL_START_KEYWORDS:
                s = rstart
                while True:
                    p = mm.find(kw, s, rend)
                    if p == -1:
                        break
                    starts.append(p)
                    s = p + 1
            starts = sorted(set(starts))
            con.debug(f"  {len(starts)} candidate SQL start offsets")

            # Pre-compute statement-boundary positions in the region. We DROP
            # `\x00` (way too dense in binary data) and `\n` (also too dense in
            # binary data); only `;` is reliable. As a backup, we'll also fall
            # back to a brute L-range search for migrations that end without `;`.
            boundaries: list[int] = []
            s = rstart
            while True:
                p = mm.find(b";", s, rend)
                if p == -1:
                    break
                boundaries.append(p)
                s = p + 1
            boundaries.sort()
            con.debug(f"  {len(boundaries)} ';' boundary positions")

            anchors: list[BinaryAnchor] = []
            import bisect

            for sp in starts:
                search_end = min(rend - SHA384_LEN, sp + MAX_MIGRATION_SQL_LEN)
                if search_end <= sp + 40:
                    continue
                # Find boundaries in (sp+40, search_end] using binary search.
                lo = bisect.bisect_left(boundaries, sp + 40)
                hi = bisect.bisect_right(boundaries, search_end)
                tried = 0
                matched = False
                for bp in boundaries[lo:hi]:
                    # End-of-SQL might be exactly at `bp`, or just after (to include
                    # the boundary character itself). Try a small set of offsets.
                    for offset in (0, 1, 2, 3):
                        L = bp - sp + offset
                        if L < 40 or sp + L + SHA384_LEN > rend:
                            continue
                        tried += 1
                        sql_bytes = mm[sp : sp + L]
                        expected = mm[sp + L : sp + L + SHA384_LEN]
                        if hashlib.sha384(sql_bytes).digest() == expected:
                            first_line_bytes = mm[sp : sp + min(200, L)].split(b"\n", 1)[0]
                            anchors.append(
                                BinaryAnchor(
                                    offset=sp,
                                    sql_len=L,
                                    checksum_hex=expected.hex().upper(),
                                    sql_first_line=first_line_bytes.decode(
                                        "utf-8", errors="replace"
                                    ).strip(),
                                    sql_full_lower=sql_bytes.decode(
                                        "utf-8", errors="replace"
                                    ).lower(),
                                )
                            )
                            matched = True
                            break
                    if matched:
                        break
            # Deduplicate by offset (just in case multiple SQL_START keywords matched).
            seen: set[int] = set()
            unique: list[BinaryAnchor] = []
            for a in sorted(anchors, key=lambda a: a.offset):
                if a.offset in seen:
                    continue
                seen.add(a.offset)
                unique.append(a)
            con.debug(f"  found {len(unique)} (sql, sha384) anchors")
            return unique
        finally:
            mm.close()


def find_description_near(binary: Path, description: str, anchors: list[BinaryAnchor]) -> Optional[BinaryAnchor]:
    """Look for `description` byte sequence in binary; return the nearest anchor."""
    needle = description.encode("utf-8")
    if not needle:
        return None
    with binary.open("rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            # Find all positions of this description
            positions: list[int] = []
            s = 0
            while True:
                p = mm.find(needle, s)
                if p == -1:
                    break
                positions.append(p)
                s = p + 1
            if not positions:
                return None
            # For each position, find the closest anchor
            best: Optional[BinaryAnchor] = None
            best_dist = float("inf")
            for pos in positions:
                for a in anchors:
                    # description usually precedes SQL in the struct; allow
                    # both directions within ~500 bytes
                    dist = abs(a.offset - pos)
                    if dist < 5000 and dist < best_dist:
                        best = a
                        best_dist = dist
            return best
        finally:
            mm.close()


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


_DESC_STOPWORDS = frozenset({
    "add", "drop", "create", "alter", "index", "table", "column",
    "set", "use", "with", "from", "into", "and", "the", "for",
    "has", "have", "pragma", "select", "insert", "update", "delete",
    "primary", "key", "not", "null", "default", "where", "rollout",
})


def _significant_tokens(description: str) -> list[str]:
    """Lowercase tokens >= 3 chars, excluding generic SQL/English stopwords."""
    if not description:
        return []
    return [
        t.lower()
        for t in description.replace("_", " ").split()
        if len(t) >= 3 and t.lower() not in _DESC_STOPWORDS
    ]


def _description_matches_anchor(description: str, anchor: BinaryAnchor) -> bool:
    """At least one significant description token appears in the anchor's full SQL.

    Tokens are matched as plain substrings against the lowercased full SQL, so
    'thread' matches inside 'thread_id', 'usage' inside 'usage_count', etc.
    An empty token list (description is all-stopwords) passes neutrally.
    """
    tokens = _significant_tokens(description)
    if not tokens:
        return True
    sql = anchor.sql_full_lower
    for t in tokens:
        if t in sql or t.replace(" ", "_") in sql:
            return True
    return False


def match_db_rows_to_anchors(
    db_path: Path,
    db_rows: list[MigrationRow],
    binary: Path,
    anchors: list[BinaryAnchor],
) -> list[tuple[MigrationRow, Optional[BinaryAnchor]]]:
    """For each DB migration row, find the matching binary anchor.

    Two-pass strategy:
      1. Exact checksum match (no drift; trivial case).
      2. Version-order greedy: sqlx stores each DB's migrations contiguously in
         the binary in version order, so we walk anchors in offset order and
         assign each unmatched DB row to the next anchor whose full SQL contains
         at least one significant token from the DB row's description. If no
         content-match anchor is found between the cursor and the next exact
         match, fall back to the immediate-next position. Each anchor is used at
         most once.

    `binary` is no longer needed by this function (full SQL is on the anchor),
    but the signature is preserved for callers.
    """
    del binary  # kept in signature for API stability
    anchors_sorted = sorted(anchors, key=lambda a: a.offset)
    by_cksum: dict[str, BinaryAnchor] = {a.checksum_hex: a for a in anchors_sorted}
    matched: list[tuple[MigrationRow, Optional[BinaryAnchor]]] = []
    used_offsets: set[int] = set()

    # First pass: exact checksum match.
    for row in db_rows:
        a = by_cksum.get(row.checksum_hex)
        if a is not None:
            matched.append((row, a))
            used_offsets.add(a.offset)
        else:
            matched.append((row, None))

    # Second pass: version-order greedy for unmatched rows.
    # The cursor is the offset of the most recently matched anchor; the next row
    # must match at a strictly greater offset.
    cursor_offset = -1
    for i, (row, m) in enumerate(matched):
        if m is not None:
            if m.offset > cursor_offset:
                cursor_offset = m.offset
            continue
        # Find first available anchor with offset > cursor that content-matches.
        chosen: Optional[BinaryAnchor] = None
        for a in anchors_sorted:
            if a.offset <= cursor_offset or a.offset in used_offsets:
                continue
            if _description_matches_anchor(row.description, a):
                chosen = a
                break
        # Position fallback: if no content match, take the immediate-next available.
        if chosen is None:
            for a in anchors_sorted:
                if a.offset > cursor_offset and a.offset not in used_offsets:
                    chosen = a
                    break
        if chosen is not None:
            matched[i] = (row, chosen)
            used_offsets.add(chosen.offset)
            cursor_offset = chosen.offset

    return matched


def expected_columns_from_sql(binary: Path, anchor: BinaryAnchor) -> set[str]:
    """Best-effort extraction of column names a CREATE TABLE migration introduces."""
    with binary.open("rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            sql = mm[anchor.offset : anchor.offset + anchor.sql_len].decode(
                "utf-8", errors="replace"
            )
        finally:
            mm.close()
    cols: set[str] = set()
    # Very lightweight parse: look for `CREATE TABLE ... (col1 ..., col2 ..., ...)`
    # We also handle `ALTER TABLE ... ADD COLUMN <name>`.
    upper = sql.upper()
    if "CREATE TABLE" in upper:
        try:
            paren_open = sql.index("(")
            depth = 0
            chunk: list[str] = []
            i = paren_open
            buf: list[str] = []
            while i < len(sql):
                ch = sql[i]
                if ch == "(":
                    depth += 1
                    if depth > 1:
                        buf.append(ch)
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        chunk.append("".join(buf))
                        break
                    buf.append(ch)
                elif ch == "," and depth == 1:
                    chunk.append("".join(buf))
                    buf = []
                else:
                    buf.append(ch)
                i += 1
            for part in chunk:
                part = part.strip()
                if not part:
                    continue
                # Skip table-level constraints
                head = part.split(None, 1)[0].strip("`\"").upper()
                if head in {
                    "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "INDEX",
                }:
                    continue
                col_name = part.split(None, 1)[0].strip("`\"")
                if col_name and col_name.upper() not in {"--"}:
                    cols.add(col_name)
        except (ValueError, IndexError):
            pass
    elif "ADD COLUMN" in upper:
        # ALTER TABLE foo ADD COLUMN bar ...
        try:
            after = sql[upper.index("ADD COLUMN") + len("ADD COLUMN") :].strip()
            col_name = after.split(None, 1)[0].strip("`\";,")
            if col_name:
                cols.add(col_name)
        except (ValueError, IndexError):
            pass
    return cols


def _table_dropped_or_rebuilt_later(
    table_name: str,
    anchor: BinaryAnchor,
    all_anchors: list[BinaryAnchor],
) -> bool:
    """Return True if some anchor at a later offset drops, renames, or rebuilds the table.

    Detects:
      * `DROP TABLE <name>` / `DROP TABLE IF EXISTS <name>`
      * `ALTER TABLE <name> RENAME TO ...`  (rebuild idiom for sqlite column renames:
        rename, recreate, copy, drop_old)
    Comparison is on the lowercased full SQL so e.g. state_5 m23's
    `PRAGMA auto_vacuum = INCREMENTAL;\\n\\nDROP TABLE IF EXISTS logs;` is matched
    even though its first line is just the PRAGMA.
    """
    tn = table_name.lower()
    for a in all_anchors:
        if a.offset <= anchor.offset:
            continue
        sql = a.sql_full_lower
        if (
            f"drop table {tn}" in sql
            or f"drop table if exists {tn}" in sql
            or f"alter table {tn} rename to" in sql
        ):
            return True
    return False


def check_schema_compat(
    db_path: Path,
    anchor: BinaryAnchor,
    binary: Path,
    all_anchors: Optional[list[BinaryAnchor]] = None,
) -> tuple[bool, list[str]]:
    """Verify that the table targeted by `anchor`'s SQL exists with all expected columns.

    Returns (ok, notes). `ok=True` means it's safe to update this migration's checksum
    without re-running the SQL (the schema is already at-or-newer, or a later
    applied migration has since dropped or rebuilt the table the binary's SQL
    targets).
    """
    notes: list[str] = []
    expected_cols = expected_columns_from_sql(binary, anchor)
    if not expected_cols:
        # Not a CREATE TABLE / ALTER TABLE ADD COLUMN — likely an UPDATE or
        # other data-only migration. We can't easily verify; default to OK
        # since these don't change schema. Mark as note.
        notes.append("non-schema migration (likely data-only); checksum-only fix is safe")
        return (True, notes)

    # Guess table name from first line
    first_upper = anchor.sql_first_line.upper()
    table_name: Optional[str] = None
    for kw in ("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", "ALTER TABLE"):
        if kw in first_upper:
            after = first_upper[first_upper.index(kw) + len(kw) :].strip()
            # Next token is the table name
            token = after.split(None, 1)[0]
            table_name = token.strip("`\";,()").lower()
            break
    if not table_name:
        notes.append("could not parse table name; skipping schema check")
        return (True, notes)

    try:
        actual_cols = {c[0].lower() for c in read_table_columns(db_path, table_name)}
    except sqlite3.Error as exc:
        notes.append(f"table_info({table_name}) failed: {exc}")
        return (False, notes)
    if not actual_cols:
        if all_anchors and _table_dropped_or_rebuilt_later(table_name, anchor, all_anchors):
            notes.append(
                f"table '{table_name}' missing, but a later migration drops or rebuilds it; "
                f"checksum-only fix is safe"
            )
            return (True, notes)
        notes.append(f"table '{table_name}' missing — migration not applied yet")
        return (False, notes)

    missing = {c.lower() for c in expected_cols} - actual_cols
    if missing:
        if all_anchors and _table_dropped_or_rebuilt_later(table_name, anchor, all_anchors):
            notes.append(
                f"table '{table_name}' missing columns {sorted(missing)}, "
                f"but a later migration rebuilds it (cols renamed/dropped); safe"
            )
            return (True, notes)
        notes.append(
            f"table '{table_name}' missing columns: {sorted(missing)} "
            f"(actual schema older than binary expects)"
        )
        return (False, notes)
    notes.append(f"table '{table_name}' has all {len(expected_cols)} expected columns")
    return (True, notes)


def compute_checksum_diffs(
    db_path: Path, binary: Path, anchors: list[BinaryAnchor]
) -> list[ChecksumDiff]:
    """Compare DB's _sqlx_migrations checksums against the binary's anchors."""
    rows = read_migrations(db_path)
    matched = match_db_rows_to_anchors(db_path, rows, binary, anchors)
    diffs: list[ChecksumDiff] = []
    for row, anchor in matched:
        if anchor is None:
            diffs.append(
                ChecksumDiff(
                    db_path=db_path,
                    db_row=row,
                    binary_anchor=None,
                    schema_ok=False,
                    schema_notes=["no matching binary anchor found"],
                )
            )
            continue
        if row.checksum_hex == anchor.checksum_hex:
            continue  # match, no drift
        ok, notes = check_schema_compat(db_path, anchor, binary, all_anchors=anchors)
        diffs.append(
            ChecksumDiff(
                db_path=db_path,
                db_row=row,
                binary_anchor=anchor,
                schema_ok=ok,
                schema_notes=notes,
            )
        )
    return diffs


# ---------------------------------------------------------------------------
# Backfill analysis
# ---------------------------------------------------------------------------


def collect_backfill_status(codex_home: Path, state_db: Path) -> BackfillStatus:
    bf = read_backfill_state(state_db) or ("unknown", None, None)
    indexed = read_indexed_rollout_paths(state_db)
    threads_count = read_threads_count(state_db)
    scheme = detect_rollout_path_scheme(state_db)

    sessions_dir = codex_home / "sessions"
    archived_dir = codex_home / "archived_sessions"
    all_files: list[Path] = []
    if sessions_dir.is_dir():
        all_files.extend(p for p in sessions_dir.rglob("*.jsonl"))
    if archived_dir.is_dir():
        all_files.extend(p for p in archived_dir.rglob("*.jsonl"))

    unindexed: list[Path] = []
    for fp in all_files:
        rp = windows_path_to_rollout(fp, scheme)
        if rp not in indexed:
            unindexed.append(fp)

    return BackfillStatus(
        status=bf[0],
        last_watermark=bf[1],
        last_success_at=bf[2],
        indexed_threads=threads_count,
        sessions_jsonl_count=sum(1 for p in all_files if p.is_relative_to(sessions_dir))
        if sessions_dir.is_dir()
        else 0,
        archived_jsonl_count=len(all_files) - (
            sum(1 for p in all_files if p.is_relative_to(sessions_dir)) if sessions_dir.is_dir() else 0
        ),
        unindexed_files=unindexed,
    )


# ---------------------------------------------------------------------------
# Backup & WAL handling
# ---------------------------------------------------------------------------


def backup_db(db: Path, label: str) -> Path:
    """Copy db + its -wal + -shm to a timestamped backup."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak_main = db.with_name(f"{db.name}.bak-{label}-{ts}")
    shutil.copy2(db, bak_main)
    for ext in ("-wal", "-shm"):
        side = db.with_name(db.name + ext)
        if side.exists():
            shutil.copy2(side, bak_main.with_name(bak_main.name + ext))
    return bak_main


def isolate_copy(db: Path, dest_dir: Path) -> Path:
    """Copy db + wal/shm to dest_dir for read-only dry-run. Returns new main path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_main = dest_dir / db.name
    shutil.copy2(db, new_main)
    for ext in ("-wal", "-shm"):
        side = db.with_name(db.name + ext)
        if side.exists():
            shutil.copy2(side, dest_dir / (db.name + ext))
    return new_main


# ---------------------------------------------------------------------------
# Subcommand: doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).resolve()
    binary = Path(args.binary).resolve() if args.binary else find_backend_binary(codex_home)

    con.header(f"codex-repair v{SCRIPT_VERSION} :: doctor")
    con.section("Environment")
    con.info(f"CODEX_HOME = {codex_home}")
    if binary:
        con.info(f"backend    = {binary}")
        st = binary.stat()
        con.info(f"             ({st.st_size:,} bytes, modified {time.ctime(st.st_mtime)})")
    else:
        con.err("no backend binary found — cannot extract expected checksums")
        return EXIT_NO_BINARY

    state_db = codex_home / STATE_DB_NAME
    logs_db = codex_home / LOGS_DB_NAME
    for db in (state_db, logs_db):
        if not db.exists():
            con.err(f"missing database: {db}")
            return EXIT_NO_DB
        con.info(f"db         = {db.name} ({db.stat().st_size:,} bytes)")

    # If using isolated copies for safety, copy them now.
    if args.use_isolated_copy:
        cache = Path(tempfile.mkdtemp(prefix="codex-repair-doctor-"))
        con.section(f"Isolated copy mode → {cache}")
        state_db = isolate_copy(state_db, cache)
        logs_db = isolate_copy(logs_db, cache)
        con.ok(f"copied DBs to isolated location; original DB is untouched")

    con.section("Scanning binary for migration anchors")
    t0 = time.time()
    descriptions = _collect_all_descriptions(state_db, logs_db)
    con.debug(f"  using {len(descriptions)} known descriptions as locators")
    anchors = scan_binary_anchors(binary, descriptions_hint=descriptions)
    con.ok(f"found {len(anchors)} (sql, sha384) anchors in {time.time()-t0:.1f}s")

    overall_exit = EXIT_HEALTHY
    all_diffs: list[ChecksumDiff] = []
    for db in (state_db, logs_db):
        con.section(f"Checksum drift check :: {db.name}")
        diffs = compute_checksum_diffs(db, binary, anchors)
        if not diffs:
            con.ok("all migration checksums match binary")
            continue
        for d in diffs:
            con.err(
                f"m{d.db_row.version} {d.db_row.description!r}: DB cksum "
                f"{d.db_row.checksum_hex[:16]}... ≠ binary"
            )
            if d.binary_anchor:
                con.info(f"  binary expects: {d.binary_anchor.checksum_hex[:16]}...")
                con.info(f"  binary SQL    : {d.binary_anchor.sql_first_line[:100]}")
            for note in d.schema_notes:
                con.info(f"  schema check  : {note}")
            if d.schema_ok:
                con.ok("  → SAFE to rewrite checksum (schema is compatible)")
            else:
                con.warn("  → UNSAFE: schema does not match; needs real migration run")
        all_diffs.extend(diffs)
        if any(diffs):
            overall_exit = EXIT_CHECKSUM_DRIFT

    con.section("Backfill state")
    try:
        bf = collect_backfill_status(codex_home, state_db)
    except sqlite3.Error as exc:
        con.err(f"could not read backfill_state: {exc}")
        bf = None
    if bf:
        con.info(f"status              = {bf.status}")
        con.info(f"last_watermark      = {bf.last_watermark}")
        con.info(f"indexed threads     = {bf.indexed_threads}")
        con.info(f"sessions jsonl      = {bf.sessions_jsonl_count}")
        con.info(f"archived jsonl      = {bf.archived_jsonl_count}")
        con.info(f"unindexed files     = {len(bf.unindexed_files)}")
        if bf.is_stuck:
            con.warn("backfill appears stuck (unindexed files exist or status != complete)")
            con.warn("→ on next Codex launch, 30s startup timeout may fire")
            if overall_exit == EXIT_HEALTHY:
                overall_exit = EXIT_BACKFILL_STUCK
            elif overall_exit == EXIT_CHECKSUM_DRIFT:
                overall_exit = EXIT_BOTH
        else:
            con.ok("backfill complete, no unindexed files")

    con.section("Summary")
    if overall_exit == EXIT_HEALTHY:
        con.ok("install is healthy — no action needed")
    else:
        names = {
            EXIT_CHECKSUM_DRIFT: "migration checksum drift",
            EXIT_BACKFILL_STUCK: "backfill stuck",
            EXIT_BOTH: "checksum drift + backfill stuck",
        }
        con.warn(f"detected: {names.get(overall_exit, 'unknown')}")
        con.info("to repair: python codex-repair.py fix --apply")
        con.info("dry-run :  python codex-repair.py fix             # (default)")

    return overall_exit


# ---------------------------------------------------------------------------
# Subcommand: fix-checksums
# ---------------------------------------------------------------------------


def cmd_fix_checksums(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).resolve()
    binary = Path(args.binary).resolve() if args.binary else find_backend_binary(codex_home)
    if not binary:
        con.err("no backend binary found")
        return EXIT_NO_BINARY

    con.header(f"codex-repair v{SCRIPT_VERSION} :: fix-checksums")
    state_db = codex_home / STATE_DB_NAME
    logs_db = codex_home / LOGS_DB_NAME

    con.section("Scanning binary for expected checksums...")
    descriptions = _collect_all_descriptions(state_db, logs_db)
    anchors = scan_binary_anchors(binary, descriptions_hint=descriptions)
    con.ok(f"found {len(anchors)} anchors")

    operate_on_state = state_db
    operate_on_logs = logs_db
    if args.use_isolated_copy:
        cache = Path(tempfile.mkdtemp(prefix="codex-repair-fix-"))
        con.section(f"Isolated copy mode → {cache}")
        operate_on_state = isolate_copy(state_db, cache)
        operate_on_logs = isolate_copy(logs_db, cache)
        con.ok("real DBs untouched; operating on isolated copies (forces dry-run)")
        args.apply = False

    fixes_planned: list[ChecksumDiff] = []
    for db in (operate_on_state, operate_on_logs):
        con.section(f"Checking {db.name}")
        diffs = compute_checksum_diffs(db, binary, anchors)
        if not diffs:
            con.ok("no drift")
            continue
        for d in diffs:
            tag = "✓ safe" if d.schema_ok else "✗ UNSAFE"
            con.info(
                f"  m{d.db_row.version} {d.db_row.description!r}: "
                f"{d.db_row.checksum_hex[:16]}... → "
                f"{d.binary_anchor.checksum_hex[:16] if d.binary_anchor else 'NO MATCH'}...  "
                f"[{tag}]"
            )
            for note in d.schema_notes:
                con.debug(f"    {note}")
        if any(not d.schema_ok for d in diffs):
            con.err(
                f"refusing to fix {db.name}: at least one drift has incompatible schema; "
                f"manual investigation required"
            )
            return EXIT_ERROR
        fixes_planned.extend(diffs)

    if not fixes_planned:
        con.ok("nothing to do; checksums already match")
        return EXIT_HEALTHY

    con.section(f"{'APPLYING' if args.apply else 'DRY-RUN'}: {len(fixes_planned)} checksum update(s)")
    if not args.apply:
        for d in fixes_planned:
            con.info(
                f"  would UPDATE {d.db_path.name}._sqlx_migrations "
                f"SET checksum=<binary> WHERE version={d.db_row.version}"
            )
        con.warn("dry-run only — pass --apply to actually rewrite checksums")
        return EXIT_HEALTHY

    # APPLY: backup, then UPDATE in transactions.
    by_db: dict[Path, list[ChecksumDiff]] = {}
    for d in fixes_planned:
        by_db.setdefault(d.db_path, []).append(d)
    for db, diffs in by_db.items():
        backup = backup_db(db, "fix-checksums")
        con.ok(f"backed up {db.name} → {backup.name}")
        with sqlite_rw(db) as cn:
            cur = cn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                for d in diffs:
                    assert d.binary_anchor is not None
                    cur.execute(
                        "UPDATE _sqlx_migrations SET checksum = ? WHERE version = ?",
                        (bytes.fromhex(d.binary_anchor.checksum_hex), d.db_row.version),
                    )
                    con.ok(
                        f"  {db.name} m{d.db_row.version}: checksum rewritten "
                        f"({cur.rowcount} row)"
                    )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    con.section("Verifying")
    for db in by_db:
        diffs_after = compute_checksum_diffs(db, binary, anchors)
        if diffs_after:
            con.err(f"{db.name}: {len(diffs_after)} drift(s) remain after fix")
            return EXIT_ERROR
        con.ok(f"{db.name}: all checksums now match binary")
    return EXIT_HEALTHY


# ---------------------------------------------------------------------------
# Subcommand: manual-backfill
# ---------------------------------------------------------------------------


def _iso_to_secs_ms(iso_str: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    if not iso_str:
        return (None, None)
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        s = int(dt.timestamp())
        return (s, s * 1000)
    except Exception:
        return (None, None)


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    return s.strip()[:n]


def _normalize_source(payload_source) -> str:
    """Codex sometimes stores source as a string, sometimes as a dict like
    {'subagent': {'other': 'guardian'}}. Normalize to a single string."""
    if payload_source is None:
        return "unknown"
    if isinstance(payload_source, str):
        return payload_source
    if isinstance(payload_source, dict):
        # Pick the first key as a label
        if payload_source:
            return next(iter(payload_source.keys()))
        return "unknown"
    return str(payload_source)


def _extract_first_user_message(jsonl: Path, max_lines: int = 200) -> Optional[str]:
    try:
        with jsonl.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                p = obj.get("payload", {})
                if not isinstance(p, dict):
                    continue
                if t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                    content = p.get("content", [])
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "input_text":
                            txt = c.get("text", "")
                            if txt and not txt.startswith("<"):
                                return txt
                if t == "event_msg" and p.get("type") in ("user_message", "session_user_input"):
                    msg = p.get("message") or p.get("input") or p.get("text")
                    if msg:
                        return msg if isinstance(msg, str) else str(msg)
    except OSError:
        pass
    return None


def cmd_manual_backfill(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).resolve()
    state_db = codex_home / STATE_DB_NAME

    con.header(f"codex-repair v{SCRIPT_VERSION} :: manual-backfill")
    if not state_db.exists():
        con.err(f"missing {state_db}")
        return EXIT_NO_DB

    operate_db = state_db
    if args.use_isolated_copy:
        cache = Path(tempfile.mkdtemp(prefix="codex-repair-backfill-"))
        con.section(f"Isolated copy mode → {cache}")
        operate_db = isolate_copy(state_db, cache)
        args.apply = False

    con.section("Discovering table schema (dynamic INSERT)")
    cols = read_table_columns(operate_db, "threads")
    if not cols:
        con.err("threads table not found — state_5.sqlite migration may not be applied yet")
        return EXIT_ERROR
    col_names = [c[0] for c in cols]
    not_null = {c[0] for c in cols if c[2] == 1}
    con.ok(f"threads has {len(col_names)} columns")
    con.debug(f"columns: {col_names}")
    con.debug(f"NOT NULL: {sorted(not_null)}")

    scheme = detect_rollout_path_scheme(operate_db)
    con.info(f"rollout_path scheme: {scheme}")

    indexed = read_indexed_rollout_paths(operate_db)
    con.info(f"currently indexed: {len(indexed)} threads")

    sessions_dir = codex_home / "sessions"
    archived_dir = codex_home / "archived_sessions"
    all_files: list[Path] = []
    if sessions_dir.is_dir():
        all_files.extend(sessions_dir.rglob("*.jsonl"))
    if archived_dir.is_dir():
        all_files.extend(archived_dir.rglob("*.jsonl"))
    con.info(f"jsonl files on disk: {len(all_files)} "
             f"(sessions/ + archived_sessions/)")

    unindexed_pairs: list[tuple[Path, str]] = []
    for fp in all_files:
        rp = windows_path_to_rollout(fp, scheme)
        if rp not in indexed:
            unindexed_pairs.append((fp, rp))
    con.info(f"unindexed: {len(unindexed_pairs)}")

    if not unindexed_pairs:
        con.ok("nothing to backfill")
        # Still ensure backfill_state is 'complete' if requested.
        if args.apply:
            with sqlite_rw(operate_db) as cn:
                cur = cn.cursor()
                cur.execute("SELECT status FROM backfill_state")
                cur_status = cur.fetchone()
                if cur_status and cur_status[0] != "complete":
                    now = int(time.time())
                    cur.execute(
                        "UPDATE backfill_state SET status='complete', "
                        "last_success_at=?, updated_at=? WHERE id=1",
                        (now, now),
                    )
                    cn.commit()
                    con.ok("backfill_state.status -> 'complete'")
        return EXIT_HEALTHY

    # Build rows.
    archived_root = archived_dir.resolve() if archived_dir.is_dir() else None
    planned_inserts: list[dict] = []
    skipped: list[tuple[Path, str]] = []
    for win_path, lin_path in unindexed_pairs:
        try:
            with win_path.open("r", encoding="utf-8") as f:
                first_line = f.readline()
            meta = json.loads(first_line)
            if meta.get("type") != "session_meta":
                skipped.append((win_path, "first line not session_meta"))
                continue
            payload = meta.get("payload", {}) or {}
            tid = payload.get("id")
            if not tid:
                skipped.append((win_path, "missing id"))
                continue
            ts_str = payload.get("timestamp") or meta.get("timestamp")
            created_s, created_ms = _iso_to_secs_ms(ts_str)
            if created_s is None:
                created_s = int(win_path.stat().st_mtime)
                created_ms = created_s * 1000
            updated_s = int(win_path.stat().st_mtime)
            updated_ms = updated_s * 1000

            first_msg = _extract_first_user_message(win_path) or ""
            title = _truncate(first_msg, 200) or win_path.name
            preview = _truncate(first_msg, 200)
            first_msg_db = _truncate(first_msg, 1000)
            is_archived = (
                archived_root is not None
                and win_path.resolve().is_relative_to(archived_root)
            )

            row = {
                "id": tid,
                "rollout_path": lin_path,
                "created_at": created_s,
                "updated_at": updated_s,
                "source": _normalize_source(payload.get("source")),
                "model_provider": payload.get("model_provider", "openai"),
                "cwd": payload.get("cwd", "/"),
                "title": title,
                "sandbox_policy": '{"type":"danger-full-access"}',
                "approval_mode": "never",
                "tokens_used": 0,
                "has_user_event": 1 if first_msg else 0,
                "archived": 1 if is_archived else 0,
                "archived_at": updated_s if is_archived else None,
                "git_sha": None,
                "git_branch": None,
                "git_origin_url": None,
                "cli_version": payload.get("cli_version", ""),
                "first_user_message": first_msg_db,
                "memory_mode": "enabled",
                "model": None,
                "reasoning_effort": None,
                "created_at_ms": created_ms,
                "updated_at_ms": updated_ms,
                "thread_source": payload.get("thread_source", "user"),
                "preview": preview,
            }
            # Filter to only columns that actually exist in this schema
            filtered = {k: v for k, v in row.items() if k in col_names}
            # Ensure all NOT NULL columns we know about have a value
            for nn in not_null:
                if nn in filtered and filtered[nn] is None:
                    filtered[nn] = "" if isinstance(filtered.get(nn), str) else 0
            planned_inserts.append(filtered)
        except Exception as exc:
            skipped.append((win_path, f"err: {exc}"))

    con.section(f"{'APPLYING' if args.apply else 'DRY-RUN'}: insert {len(planned_inserts)} threads")
    if skipped:
        con.warn(f"{len(skipped)} files skipped:")
        for p, r in skipped[:10]:
            con.info(f"  {p.name}: {r}")
        if len(skipped) > 10:
            con.info(f"  ... {len(skipped)-10} more")

    if not args.apply:
        for row in planned_inserts[:3]:
            con.info(f"  would INSERT id={row['id']} title={row.get('title','')[:50]!r}")
        if len(planned_inserts) > 3:
            con.info(f"  ... {len(planned_inserts)-3} more")
        con.warn("dry-run only — pass --apply to actually INSERT rows")
        return EXIT_HEALTHY

    backup = backup_db(state_db, "manual-backfill")
    con.ok(f"backed up state_5.sqlite → {backup.name}")

    inserted = 0
    with sqlite_rw(state_db) as cn:
        cur = cn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for row in planned_inserts:
                placeholders = ", ".join("?" for _ in row)
                cols_sql = ", ".join(row.keys())
                cur.execute(
                    f"INSERT OR IGNORE INTO threads ({cols_sql}) VALUES ({placeholders})",
                    list(row.values()),
                )
                inserted += cur.rowcount
            # mark backfill complete
            newest = max(all_files, key=lambda p: p.stat().st_mtime, default=None)
            watermark = None
            if newest:
                rel = newest.relative_to(codex_home).as_posix()
                watermark = rel
            now = int(time.time())
            cur.execute(
                "UPDATE backfill_state SET status='complete', "
                "last_watermark=COALESCE(?, last_watermark), "
                "last_success_at=?, updated_at=? WHERE id=1",
                (watermark, now, now),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    con.section("Result")
    con.ok(f"inserted {inserted} new thread row(s)")
    final_count = read_threads_count(state_db)
    con.ok(f"threads table now has {final_count} rows")
    return EXIT_HEALTHY


# ---------------------------------------------------------------------------
# Subcommand: extract-checksums
# ---------------------------------------------------------------------------


def cmd_extract_checksums(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).resolve()
    binary = Path(args.binary).resolve() if args.binary else find_backend_binary(codex_home)
    if not binary:
        con.err("no backend binary found")
        return EXIT_NO_BINARY

    con.header(f"codex-repair v{SCRIPT_VERSION} :: extract-checksums")
    con.info(f"binary = {binary}")
    # Use DB descriptions as locator if DBs exist; otherwise fallback algorithm runs.
    descriptions = _collect_all_descriptions(
        codex_home / STATE_DB_NAME, codex_home / LOGS_DB_NAME
    )
    anchors = scan_binary_anchors(binary, descriptions_hint=descriptions)
    if args.json:
        out = [
            {
                "offset": a.offset,
                "sql_len": a.sql_len,
                "checksum": a.checksum_hex,
                "sql_first_line": a.sql_first_line,
            }
            for a in anchors
        ]
        print(json.dumps(out, indent=2))
    else:
        for a in anchors:
            print(f"  @{a.offset:>10}  L={a.sql_len:>5}  {a.checksum_hex[:32]}...  | {a.sql_first_line[:80]}")
    con.ok(f"found {len(anchors)} anchors")
    return EXIT_HEALTHY


# ---------------------------------------------------------------------------
# Subcommand: fix (auto)
# ---------------------------------------------------------------------------


def cmd_fix(args: argparse.Namespace) -> int:
    """Auto-detect & repair both issues."""
    # 1) doctor pass
    doctor_args = argparse.Namespace(**vars(args))
    rc = cmd_doctor(doctor_args)
    if rc == EXIT_HEALTHY:
        con.ok("doctor says healthy — nothing to fix")
        return EXIT_HEALTHY
    if rc in (EXIT_NO_BINARY, EXIT_NO_DB):
        return rc

    # 2) fix checksums if needed
    if rc in (EXIT_CHECKSUM_DRIFT, EXIT_BOTH):
        con.header("Phase 1: fix migration checksums")
        sub_args = argparse.Namespace(**vars(args))
        rc1 = cmd_fix_checksums(sub_args)
        if rc1 != EXIT_HEALTHY:
            return rc1

    # 3) manual backfill if needed
    if rc in (EXIT_BACKFILL_STUCK, EXIT_BOTH):
        con.header("Phase 2: manual backfill")
        sub_args = argparse.Namespace(**vars(args))
        rc2 = cmd_manual_backfill(sub_args)
        if rc2 != EXIT_HEALTHY:
            return rc2

    return EXIT_HEALTHY


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def _add_global_flags(p: argparse.ArgumentParser) -> None:
    """Add the global flags to a (sub)parser so they work in any position."""
    p.add_argument(
        "--codex-home",
        default=str(DEFAULT_CODEX_HOME),
        help=f"Codex home directory (default: {DEFAULT_CODEX_HOME})",
    )
    p.add_argument(
        "--binary",
        default=None,
        help="Backend binary path (default: auto-detect newest in {codex-home}/bin/wsl/*/codex)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify databases (default: dry-run)",
    )
    p.add_argument(
        "--use-isolated-copy",
        action="store_true",
        help="Copy DBs to a temp dir and operate on copies (zero risk to running Codex; implies dry-run)",
    )
    p.add_argument("-v", "--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-repair",
        description="Unified repair tool for Codex Desktop (Windows + WSL backend).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python codex-repair.py doctor\n"
            "  python codex-repair.py fix                   # dry-run\n"
            "  python codex-repair.py fix --apply           # actually repair\n"
            "  python codex-repair.py doctor --use-isolated-copy  # zero risk to running Codex\n"
        ),
    )
    _add_global_flags(p)
    sub = p.add_subparsers(dest="cmd")
    for name, help_text in (
        ("doctor", "Diagnose only (read-only)"),
        ("fix", "Auto-detect and fix all issues (dry-run unless --apply)"),
        ("fix-checksums", "Only fix migration checksum drift"),
        ("manual-backfill", "Only do manual thread metadata backfill"),
    ):
        sp = sub.add_parser(name, help=help_text)
        _add_global_flags(sp)
    extract = sub.add_parser("extract-checksums", help="List all migration checksums from binary")
    _add_global_flags(extract)
    extract.add_argument("--json", action="store_true", help="output JSON")
    return p


def _merge_global_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """When global flags appear after the subcommand, the subparser owns them.
    When they appear before, the top-level parser owns them. We just need to
    ensure all known global attrs exist on `args`; argparse already does that
    because we attached `_add_global_flags` to every subparser.
    """
    # Defensive: ensure attributes exist with their defaults if argparse missed.
    for attr, default in (
        ("codex_home", str(DEFAULT_CODEX_HOME)),
        ("binary", None),
        ("apply", False),
        ("use_isolated_copy", False),
        ("verbose", False),
    ):
        if not hasattr(args, attr):
            setattr(args, attr, default)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _merge_global_args(args, parser)

    # Route progress messages to stderr when the subcommand emits machine-
    # readable output to stdout (currently only `extract-checksums --json`),
    # so redirecting stdout to a file gives a clean data stream.
    json_mode = (args.cmd == "extract-checksums") and getattr(args, "json", False)
    global con
    con = Console(verbose=args.verbose, stream=sys.stderr if json_mode else sys.stdout)

    if args.use_isolated_copy and args.apply:
        con.warn("--use-isolated-copy implies dry-run; ignoring --apply")
        args.apply = False
    cmd = args.cmd or "doctor"
    handler = {
        "doctor": cmd_doctor,
        "fix": cmd_fix,
        "fix-checksums": cmd_fix_checksums,
        "manual-backfill": cmd_manual_backfill,
        "extract-checksums": cmd_extract_checksums,
    }.get(cmd)
    if not handler:
        parser.print_help()
        return EXIT_ERROR
    try:
        return handler(args)
    except KeyboardInterrupt:
        con.warn("interrupted")
        return EXIT_USER_ABORT
    except Exception as exc:
        con.err(f"unexpected error: {exc!r}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
