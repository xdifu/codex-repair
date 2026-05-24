#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex-repair.py — Unified repair tool for Codex Desktop (macOS, Windows, and WSL backend).

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
  db-health             Check SQLite DB health without needing a backend binary.
  recover-state-db      Try sqlite3 .recover/.dump for state_5.sqlite.
  reset-state-db        Move state_5.sqlite aside so Codex can recreate it.
  quarantine-invalid-jsonl
                        Move unindexable legacy JSONL files out of sessions/.

Run `python codex-repair.py -h` or `python codex-repair.py <cmd> -h` for help.

Safety contract:
  * Read-only by default. `--apply` is required to mutate any database.
  * `--use-isolated-copy` copies DBs to a private temp dir before reading,
    so a running Codex is never touched. Implies dry-run.
  * Every mutation is wrapped in a transaction and preceded by a timestamped
    backup of the affected database (plus its WAL/SHM if present).
  * CODEX_HOME and SQLite home are resolved separately; sessions are read from
    CODEX_HOME while state_5/logs_2/goals_1 live under SQLite home.
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
import platform
import re
import shutil
import sqlite3
import subprocess
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

SCRIPT_VERSION = "1.0.10"

# SQLite databases used by current Codex builds.
STATE_DB_NAME = "state_5.sqlite"
LOGS_DB_NAME = "logs_2.sqlite"
GOALS_DB_NAME = "goals_1.sqlite"

# Defaults used if auto-detection fails.
def default_codex_home() -> Path:
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"])
    if os.environ.get("USERPROFILE"):
        return Path(os.environ["USERPROFILE"]) / ".codex"
    return Path.home() / ".codex"


DEFAULT_CODEX_HOME = default_codex_home()

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
    """Snapshot of `backfill_state` + thread/session counts.

    `unindexed_files` contains only indexable rollout JSONL files: files whose
    first line is a valid `session_meta` record with a session id. Older Codex
    builds can leave `rollout-*.jsonl` files whose first line is not
    `session_meta`; those cannot be inserted into `threads`, so they are tracked
    separately in `ignored_unindexable_files`. A complete backfill marker is not
    treated as stuck merely because old valid files are absent from `threads`.
    """

    status: str
    last_watermark: Optional[str]
    last_success_at: Optional[int]
    indexed_threads: int
    sessions_jsonl_count: int
    archived_jsonl_count: int
    unindexed_files: list[Path]
    ignored_unindexable_files: list[tuple[Path, str]]

    @property
    def is_stuck(self) -> bool:
        # Startup only blocks while Codex's own backfill marker is incomplete.
        # Valid-but-unindexed JSONL files with status='complete' can mean stale
        # metadata, but current Codex does not treat that as a startup gate.
        return self.status != "complete"


@dataclasses.dataclass(frozen=True)
class DbSpec:
    """A Codex SQLite database known to this tool."""

    filename: str
    required: bool
    label: str


DB_SPECS: tuple[DbSpec, ...] = (
    DbSpec(STATE_DB_NAME, True, "state"),
    DbSpec(LOGS_DB_NAME, True, "logs"),
    DbSpec(GOALS_DB_NAME, False, "goals"),
)


@dataclasses.dataclass(frozen=True)
class CodexPaths:
    """Resolved Codex data paths.

    `CODEX_HOME` owns config/auth/sessions. `sqlite_home` owns the runtime
    SQLite databases. They are often the same directory on older installs, but
    current Windows+WSL installs can intentionally split them.
    """

    codex_home: Path
    sqlite_home: Path
    sqlite_home_source: str
    codex_home_input: Path
    sqlite_home_input: Path

    @property
    def state_db(self) -> Path:
        return self.sqlite_home / STATE_DB_NAME

    @property
    def logs_db(self) -> Path:
        return self.sqlite_home / LOGS_DB_NAME

    @property
    def goals_db(self) -> Path:
        return self.sqlite_home / GOALS_DB_NAME

    @property
    def sessions_dir(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def archived_sessions_dir(self) -> Path:
        return self.codex_home / "archived_sessions"

    def db_path(self, spec: DbSpec) -> Path:
        return self.sqlite_home / spec.filename

    def existing_db_paths(self) -> list[Path]:
        return [self.db_path(spec) for spec in DB_SPECS if self.db_path(spec).exists()]


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


def host_target_triple() -> Optional[str]:
    """Return the Rust/Codex target triple for the current host, if supported."""
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        arch = "aarch64"
    elif machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    else:
        return None

    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform.startswith("linux"):
        return f"{arch}-unknown-linux-musl"
    if sys.platform.startswith("win"):
        return f"{arch}-pc-windows-msvc"
    return None


def platform_package_for_triple(triple: str) -> Optional[str]:
    """Return the npm optional-dependency package name for a Codex target triple."""
    return {
        "x86_64-unknown-linux-musl": "@openai/codex-linux-x64",
        "aarch64-unknown-linux-musl": "@openai/codex-linux-arm64",
        "x86_64-apple-darwin": "@openai/codex-darwin-x64",
        "aarch64-apple-darwin": "@openai/codex-darwin-arm64",
        "x86_64-pc-windows-msvc": "@openai/codex-win32-x64",
        "aarch64-pc-windows-msvc": "@openai/codex-win32-arm64",
    }.get(triple)


def npm_package_path(base: Path, package_name: str) -> Path:
    """Join an npm package name, including scoped packages, onto a base path."""
    scope_and_name = package_name.split("/", 1)
    if len(scope_and_name) == 2 and scope_and_name[0].startswith("@"):
        return base / scope_and_name[0] / scope_and_name[1]
    return base / package_name


def _read_prefix(path: Path, n: int = 20_000) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(n)
    except OSError:
        return b""


def is_native_executable(path: Path) -> bool:
    """Best-effort test for a native executable rather than a shell/Node wrapper."""
    head = _read_prefix(path, 8)
    native_magics = (
        b"\xfe\xed\xfa\xce",  # Mach-O 32-bit BE
        b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit BE
        b"\xce\xfa\xed\xfe",  # Mach-O 32-bit LE
        b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit LE
        b"\xca\xfe\xba\xbe",  # Mach-O fat
        b"\xbe\xba\xfe\xca",  # Mach-O fat reversed
        b"\x7fELF",              # Linux / WSL
        b"MZ",                    # Windows PE
    )
    return any(head.startswith(magic) for magic in native_magics)


def looks_like_codex_node_wrapper(path: Path) -> bool:
    """Return True if `path` appears to be Codex's JS/npm launcher."""
    chunk = _read_prefix(path, 20_000)
    if not chunk or b"\x00" in chunk[:512]:
        return False
    needles = (
        b"@openai/codex-darwin-arm64",
        b"@openai/codex-darwin-x64",
        b"@openai/codex-linux-x64",
        b"@openai/codex-linux-arm64",
        b"@openai/codex-win32-x64",
        b"PLATFORM_PACKAGE_BY_TARGET",
        b"targetTriple",
        b"vendorRoot",
    )
    if any(n in chunk for n in needles):
        return True
    # nvm/npm shims can be shell wrappers. Treat tiny text executables named
    # codex as wrappers so they do not beat the native binary.
    sample = chunk[:512]
    printable = sum(32 <= b <= 126 or b in (9, 10, 13) for b in sample)
    mostly_text = bool(sample) and printable / len(sample) > 0.90
    lower = sample.lower()
    return mostly_text and any(tok in lower for tok in (b"node", b"npm", b"/bin/sh", b"javascript"))


def codex_package_root_from_wrapper(wrapper: Path) -> Optional[Path]:
    """Infer the @openai/codex npm package root from a wrapper path."""
    try:
        p = wrapper.resolve()
    except OSError:
        p = wrapper

    if p.parent.name == "bin" and p.parent.parent.name == "codex":
        return p.parent.parent

    parts = p.parts
    for i in range(len(parts) - 1):
        if parts[i] == "@openai" and parts[i + 1] == "codex":
            return Path(*parts[: i + 2])
    return None


def native_candidates_from_npm_wrapper(wrapper: Path) -> list[Path]:
    """Resolve Codex's npm/Node launcher to the platform native binary.

    Current npm packages use a thin JS wrapper that spawns a Rust binary from
    an optional platform package. On macOS these look like either:
        @openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex
    or the older layout:
        @openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex/codex
    """
    triple = host_target_triple()
    if not triple:
        return []
    pkg = platform_package_for_triple(triple)
    if not pkg:
        return []
    binary_name = "codex.exe" if sys.platform.startswith("win") else "codex"
    root = codex_package_root_from_wrapper(wrapper)
    if not root:
        return []

    vendor_roots: list[Path] = []
    vendor_roots.append(root / "vendor")
    vendor_roots.append(npm_package_path(root / "node_modules", pkg) / "vendor")
    if root.parent.name == "@openai":
        vendor_roots.append(root.parent / pkg.split("/", 1)[1] / "vendor")
    vendor_roots.append(npm_package_path(root.parent.parent, pkg) / "vendor")

    out: list[Path] = []
    for vendor_root in vendor_roots:
        out.append(vendor_root / triple / "bin" / binary_name)
        out.append(vendor_root / triple / "codex" / binary_name)
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in out:
        try:
            key = p.resolve() if p.exists() else p
        except OSError:
            key = p
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def find_backend_binary(codex_home: Path) -> Optional[Path]:
    """Return the best real Codex backend binary, or None.

    The original script only checked Windows/WSL paths. On macOS, `which codex`
    often points at a tiny npm/Node launcher such as:
        .../.nvm/versions/node/.../bin/codex
    That launcher is not the database-owning Rust backend and contains no sqlx
    migration checksums. This detector resolves that launcher to Codex's native
    optional-dependency binary first.
    """
    candidates: list[tuple[int, float, Path, str]] = []
    seen: set[Path] = set()

    def add_candidate(cand: Path, score: int, reason: str) -> None:
        try:
            if not cand.is_file():
                return
            resolved = cand.resolve()
            if resolved in seen:
                return
            seen.add(resolved)
            size = resolved.stat().st_size
            if is_native_executable(resolved):
                score += 1000
                reason += "; native executable"
            elif looks_like_codex_node_wrapper(resolved):
                score -= 1500
                reason += "; wrapper/launcher"
            elif size < 128_000:
                score -= 300
                reason += f"; small ({size:,} bytes)"
            else:
                score += min(size // 1_000_000, 50)
                reason += f"; size {size:,}"
            candidates.append((score, resolved.stat().st_mtime, resolved, reason))
        except OSError:
            return

    def add_path_and_resolved_native(path: Path, score: int, reason: str) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if looks_like_codex_node_wrapper(resolved):
            for native in native_candidates_from_npm_wrapper(resolved):
                add_candidate(native, score + 200, reason + "; resolved from npm wrapper")
        add_candidate(path, score, reason)

    # Codex Desktop / Windows+WSL local backend cache.
    bin_dir = codex_home / "bin"
    if bin_dir.is_dir():
        for cand in bin_dir.rglob("*"):
            if cand.name in {"codex", "codex.exe"} or cand.name.startswith("codex-"):
                add_path_and_resolved_native(cand, 100, "CODEX_HOME/bin")

    # macOS desktop app resources and Homebrew cask-style direct binaries.
    for root in (
        Path("/Applications/Codex.app/Contents/Resources"),
        Path("/opt/homebrew/Caskroom/codex"),
        Path("/usr/local/Caskroom/codex"),
    ):
        if root.is_dir():
            for cand in root.rglob("codex*"):
                if cand.name in {"codex", "codex.exe"} or cand.name.startswith("codex-"):
                    add_path_and_resolved_native(cand, 80, str(root))

    # Fallback: if Codex is on PATH, resolve the wrapper to the native binary.
    path_cand = shutil.which("codex")
    if path_cand:
        add_path_and_resolved_native(Path(path_cand), 60, "PATH")

    if not candidates:
        return None
    candidates.sort(reverse=True)
    for score, _mtime, path, reason in candidates:
        con.debug(f"backend candidate score={score}: {path} ({reason})")
    native_candidates = [c for c in candidates if is_native_executable(c[2])]
    if native_candidates:
        native_candidates.sort(reverse=True)
        return native_candidates[0][2]
    # Do not return a known wrapper just because it is the only thing on PATH.
    if looks_like_codex_node_wrapper(candidates[0][2]):
        return None
    return candidates[0][2]

def _expand_path(raw: str | Path, base: Optional[Path] = None) -> Path:
    """Expand env/user markers and resolve a path without requiring it to exist."""
    text = os.path.expandvars(os.path.expanduser(str(raw)))
    p = Path(text)
    if not p.is_absolute() and base is not None:
        p = base / p
    return p.resolve(strict=False)


def _read_config_sqlite_home(codex_home: Path) -> Optional[str]:
    """Return top-level `sqlite_home` from config.toml, if present.

    Codex's config parser supports TOML. Python 3.11+ has `tomllib`; for
    Python 3.10 we keep a narrow fallback that only accepts a top-level
    quoted string assignment.
    """
    config = codex_home / "config.toml"
    if not config.is_file():
        return None

    try:
        import tomllib  # type: ignore[attr-defined]

        with config.open("rb") as f:
            data = tomllib.load(f)
        value = data.get("sqlite_home")
        return value if isinstance(value, str) and value.strip() else None
    except ModuleNotFoundError:
        pass
    except Exception as exc:
        con.debug(f"could not parse {config} with tomllib: {exc}")
        return None

    try:
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                return None
            m = re.match(r"""^sqlite_home\s*=\s*(['"])(.*?)\1\s*(?:#.*)?$""", stripped)
            if m:
                return m.group(2).strip() or None
    except OSError as exc:
        con.debug(f"could not read {config}: {exc}")
    return None


def resolve_codex_paths(args: argparse.Namespace) -> CodexPaths:
    """Resolve Codex home and SQLite home using Codex-compatible precedence."""
    codex_home_input = Path(args.codex_home).expanduser()
    codex_home = _expand_path(codex_home_input)

    sqlite_arg = getattr(args, "sqlite_home", None)
    if sqlite_arg:
        sqlite_home_input = Path(sqlite_arg).expanduser()
        sqlite_home = _expand_path(sqlite_home_input)
        source = "cli"
    else:
        config_sqlite_home = _read_config_sqlite_home(codex_home)
        if config_sqlite_home:
            sqlite_home_input = Path(config_sqlite_home).expanduser()
            sqlite_home = _expand_path(sqlite_home_input, base=codex_home)
            source = "config"
        elif os.environ.get("CODEX_SQLITE_HOME", "").strip():
            sqlite_home_input = Path(os.environ["CODEX_SQLITE_HOME"]).expanduser()
            sqlite_home = _expand_path(sqlite_home_input)
            source = "env"
        elif (codex_home / "sqlite" / STATE_DB_NAME).exists():
            sqlite_home_input = codex_home / "sqlite"
            sqlite_home = sqlite_home_input.resolve(strict=False)
            source = "legacy"
        else:
            sqlite_home_input = codex_home
            sqlite_home = codex_home
            source = "default"

    return CodexPaths(
        codex_home=codex_home,
        sqlite_home=sqlite_home,
        sqlite_home_source=source,
        codex_home_input=codex_home_input,
        sqlite_home_input=sqlite_home_input,
    )


def resolve_db_paths(codex_home: Path, sqlite_home: Optional[Path] = None) -> tuple[Path, Path]:
    """Backward-compatible helper for callers that only need state/log DBs."""
    if sqlite_home is None:
        sqlite_home = codex_home / "sqlite" if (codex_home / "sqlite" / STATE_DB_NAME).exists() else codex_home
    return sqlite_home / STATE_DB_NAME, sqlite_home / LOGS_DB_NAME


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        text = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        return "microsoft" in text or "wsl" in text
    except OSError:
        return False


def is_mnt_drive_path(path: Path) -> bool:
    candidates = [path.as_posix()]
    try:
        candidates.append(path.resolve(strict=False).as_posix())
    except OSError:
        pass
    return any(bool(re.match(r"^/mnt/[A-Za-z](?:/|$)", p)) for p in candidates)


def wsl_sqlite_layout_status(paths: CodexPaths) -> str:
    """Classify WSL SQLite layout as 'risk', 'split', or 'none'."""
    if not is_wsl():
        return "none"

    codex_mnt = is_mnt_drive_path(paths.codex_home)
    sqlite_mnt = is_mnt_drive_path(paths.sqlite_home)

    if sqlite_mnt:
        return "risk"
    if codex_mnt:
        return "split"
    return "none"


def emit_wsl_sqlite_layout_note(paths: CodexPaths) -> None:
    """Warn about Windows/WSL shared SQLite layouts tracked in #24348."""
    status = wsl_sqlite_layout_status(paths)
    codex_symlink_to_mnt = (
        paths.codex_home_input.is_symlink()
        and is_mnt_drive_path(paths.codex_home_input.resolve(strict=False))
    )

    if status == "risk":
        con.warn("Detected WSL using SQLite state on /mnt/<drive>.")
        con.warn("This matches the Windows/WSL shared SQLite risk tracked in openai/codex#24348.")
        con.warn(
            "Checksum repair may be temporary; prefer a WSL-native CODEX_SQLITE_HOME "
            "such as /home/<user>/.codex-sqlite."
        )
        if codex_symlink_to_mnt:
            con.info("CODEX_HOME input is a symlink to /mnt/<drive>, so SQLite is shared with Windows.")
    elif status == "split":
        con.ok("WSL split-state layout detected: sessions/config are shared, SQLite is WSL-native.")


def detect_rollout_path_scheme(state_db: Path) -> str:
    """Return the path scheme stored in threads.rollout_path samples.

    Known values:
      * '/mnt/c/' for WSL-style paths
      * 'windows' for drive-letter paths like C:/...
      * 'posix' for macOS/Linux paths like /Users/... or /home/...

    If the table is empty, default to POSIX on non-Windows hosts and WSL-style
    on native Windows, matching the old behavior for Windows users.
    """
    fallback = "/mnt/c/" if os.name == "nt" else "posix"
    try:
        con_ro = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        cur = con_ro.cursor()
        cur.execute("SELECT rollout_path FROM threads WHERE rollout_path IS NOT NULL LIMIT 5")
        rows = cur.fetchall()
        con_ro.close()
    except sqlite3.Error:
        return fallback
    for (p,) in rows:
        if p and p.startswith("/mnt/"):
            return "/mnt/c/"
        if p and len(p) > 2 and p[1] == ":":
            return "windows"
        if p and p.startswith("/"):
            return "posix"
    return fallback


def windows_path_to_rollout(win_path: Path, scheme: str) -> str:
    """Convert a filesystem path to the scheme stored in threads.rollout_path."""
    p = str(win_path).replace("\\", "/")
    if scheme == "/mnt/c/":
        if len(p) > 1 and p[1] == ":":
            return "/mnt/" + p[0].lower() + p[2:]
        return p
    # 'windows' and 'posix': keep the path as-is, normalized to forward slashes.
    return p


def path_is_relative_to(path: Path, parent: Path) -> bool:
    """Compatibility helper for Python 3.7/3.8, before Path.is_relative_to()."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


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
        for L in range(15, max_L):
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
                if search_end <= sp + 15:
                    continue
                # Find boundaries in (sp+15, search_end] using binary search.
                lo = bisect.bisect_left(boundaries, sp + 15)
                hi = bisect.bisect_right(boundaries, search_end)
                tried = 0
                matched = False
                for bp in boundaries[lo:hi]:
                    # End-of-SQL might be exactly at `bp`, or just after (to include
                    # the boundary character itself). Try a small set of offsets.
                    for offset in (0, 1, 2, 3):
                        L = bp - sp + offset
                        if L < 15 or sp + L + SHA384_LEN > rend:
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


def classify_session_jsonl(jsonl: Path) -> tuple[bool, str]:
    """Return (is_indexable_session_rollout, reason).

    Codex thread backfill can only index rollout files whose first line is a
    `session_meta` JSON object with a payload id. Some legacy or partial
    `rollout-*.jsonl` files contain event records first; those should not keep
    doctor in a permanent "backfill stuck" state.
    """
    try:
        with jsonl.open("r", encoding="utf-8") as f:
            first_line = f.readline()
    except OSError as exc:
        return (False, f"could not read: {exc}")
    if not first_line.strip():
        return (False, "empty file")
    try:
        meta = json.loads(first_line)
    except json.JSONDecodeError as exc:
        return (False, f"first line invalid JSON: {exc.msg}")
    if not isinstance(meta, dict):
        return (False, "first line JSON is not an object")
    if meta.get("type") != "session_meta":
        return (False, "first line not session_meta")
    payload = meta.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return (False, "session_meta payload is not an object")
    if not payload.get("id"):
        return (False, "session_meta missing id")
    return (True, "session_meta ok")


def collect_jsonl_files(codex_home: Path) -> tuple[list[Path], Path, Path]:
    sessions_dir = codex_home / "sessions"
    archived_dir = codex_home / "archived_sessions"
    all_files: list[Path] = []
    if sessions_dir.is_dir():
        all_files.extend(p for p in sessions_dir.rglob("*.jsonl"))
    if archived_dir.is_dir():
        all_files.extend(p for p in archived_dir.rglob("*.jsonl"))
    return (all_files, sessions_dir, archived_dir)


def collect_backfill_status(codex_home: Path, state_db: Path) -> BackfillStatus:
    bf = read_backfill_state(state_db) or ("unknown", None, None)
    indexed = read_indexed_rollout_paths(state_db)
    threads_count = read_threads_count(state_db)
    scheme = detect_rollout_path_scheme(state_db)

    all_files, sessions_dir, _archived_dir = collect_jsonl_files(codex_home)

    unindexed: list[Path] = []
    ignored_unindexable: list[tuple[Path, str]] = []
    for fp in all_files:
        rp = windows_path_to_rollout(fp, scheme)
        if rp in indexed:
            continue
        is_indexable, reason = classify_session_jsonl(fp)
        if is_indexable:
            unindexed.append(fp)
        else:
            ignored_unindexable.append((fp, reason))

    session_count = (
        sum(1 for p in all_files if path_is_relative_to(p, sessions_dir))
        if sessions_dir.is_dir()
        else 0
    )

    return BackfillStatus(
        status=bf[0],
        last_watermark=bf[1],
        last_success_at=bf[2],
        indexed_threads=threads_count,
        sessions_jsonl_count=session_count,
        archived_jsonl_count=len(all_files) - session_count,
        unindexed_files=unindexed,
        ignored_unindexable_files=ignored_unindexable,
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
# SQLite health / recovery helpers
# ---------------------------------------------------------------------------


def sqlite_check(db: Path, full: bool = False) -> tuple[bool, str]:
    """Run SQLite quick_check/integrity_check against a DB opened read-only."""
    pragma = "integrity_check" if full else "quick_check"
    try:
        with sqlite_ro(db) as cn:
            cur = cn.cursor()
            cur.execute(f"PRAGMA {pragma}")
            rows = [str(r[0]) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        return (False, str(exc))
    except OSError as exc:
        return (False, str(exc))
    if rows == ["ok"]:
        return (True, "ok")
    if not rows:
        return (False, "no result returned")
    suffix = "" if len(rows) <= 8 else f"; ... {len(rows)-8} more"
    return (False, "; ".join(rows[:8]) + suffix)


def _state_db_sidecars(state_db: Path) -> list[Path]:
    return [state_db, state_db.with_name(state_db.name + "-wal"), state_db.with_name(state_db.name + "-shm")]


def _timestamped_repair_dir(codex_home: Path, prefix: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    d = codex_home / f"{prefix}-{ts}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def _copy_existing(paths: Iterable[Path], dest: Path) -> None:
    for p in paths:
        if p.exists():
            shutil.copy2(p, dest / p.name)


def _move_existing(paths: Iterable[Path], dest: Path, suffix: str = ".original") -> None:
    for p in paths:
        if p.exists():
            shutil.move(str(p), str(dest / (p.name + suffix)))


def _sqlite3_candidates(preferred: Optional[str] = None) -> list[Path]:
    """Return sqlite3 shell candidates, preferring modern/Homebrew builds.

    macOS's /usr/bin/sqlite3 can be old enough to lack the `.recover` command.
    Homebrew/MacPorts builds usually include it, so we look there first while
    still accepting an explicit --sqlite3 or SQLITE3=... override.
    """
    raw: list[Path] = []
    if preferred:
        raw.append(Path(preferred).expanduser())
    env_sqlite3 = os.environ.get("SQLITE3")
    if env_sqlite3:
        raw.append(Path(env_sqlite3).expanduser())
    for p in (
        "/opt/homebrew/opt/sqlite/bin/sqlite3",
        "/usr/local/opt/sqlite/bin/sqlite3",
        "/opt/local/bin/sqlite3",
    ):
        raw.append(Path(p))
    which = shutil.which("sqlite3")
    if which:
        raw.append(Path(which))
    raw.append(Path("/usr/bin/sqlite3"))

    out: list[Path] = []
    seen: set[Path] = set()
    for p in raw:
        try:
            if not p.is_file():
                continue
            key = p.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _sqlite3_version(sqlite3_bin: Path) -> str:
    try:
        p = subprocess.run(
            [str(sqlite3_bin), "-version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        return (p.stdout or p.stderr or "unknown").strip().splitlines()[0]
    except Exception as exc:
        return f"unknown ({exc})"


def _sqlite3_supports_recover(sqlite3_bin: Path) -> bool:
    """Best-effort check whether this sqlite3 shell supports `.recover`."""
    try:
        help_run = subprocess.run(
            [str(sqlite3_bin), "-batch", ":memory:", ".help recover"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        combined = (help_run.stdout + "\n" + help_run.stderr).lower()
        if ".recover" in combined and "unknown" not in combined:
            return True

        probe = subprocess.run(
            [str(sqlite3_bin), "-batch", ":memory:", ".recover"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        combined = (probe.stdout + "\n" + probe.stderr).lower()
        if "unknown command" in combined or "invalid arguments" in combined:
            return False
        return probe.returncode == 0 or "begin" in combined or "pragma" in combined
    except Exception:
        return False


def _load_recovery_sql(sqlite3_bin: Path, recovered_db: Path, sql_text: str, tag: str) -> tuple[bool, str]:
    load = subprocess.run(
        [str(sqlite3_bin), "-batch", str(recovered_db)],
        input=sql_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    (recovered_db.with_suffix(recovered_db.suffix + f".{tag}.load.stdout.txt")).write_text(
        load.stdout, encoding="utf-8", errors="replace"
    )
    (recovered_db.with_suffix(recovered_db.suffix + f".{tag}.load.stderr.txt")).write_text(
        load.stderr, encoding="utf-8", errors="replace"
    )
    if load.returncode != 0:
        return (False, f"loading {tag} SQL failed: {load.stderr.strip()[:500]}")

    ok, msg = sqlite_check(recovered_db, full=False)
    if not ok:
        return (False, f"{tag} DB failed quick_check: {msg}")
    return (True, "ok")


def _run_sqlite_recover(sqlite3_bin: Path, source_db: Path, recovered_db: Path, sql_file: Path) -> tuple[bool, str]:
    """Run `sqlite3 source .recover | sqlite3 recovered` and verify quick_check."""
    recover = subprocess.run(
        [str(sqlite3_bin), "-batch", str(source_db), ".recover"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sql_file.write_text(recover.stdout, encoding="utf-8", errors="replace")
    (sql_file.with_suffix(sql_file.suffix + ".stderr.txt")).write_text(
        recover.stderr, encoding="utf-8", errors="replace"
    )
    err_lower = recover.stderr.lower()
    if "unknown command" in err_lower or "invalid arguments" in err_lower:
        return (False, f"{sqlite3_bin} does not support .recover: {recover.stderr.strip()[:500]}")
    if not recover.stdout.strip():
        return (False, f"sqlite3 .recover produced no SQL; stderr: {recover.stderr.strip()[:500]}")

    return _load_recovery_sql(sqlite3_bin, recovered_db, recover.stdout, "recover")


def _run_sqlite_dump_salvage(sqlite3_bin: Path, source_db: Path, recovered_db: Path, sql_file: Path) -> tuple[bool, str]:
    """Fallback for sqlite shells without `.recover`: try `.dump` and verify.

    `.dump` is less powerful than `.recover`; it can fail if the damaged pages
    are needed to read a table. It is still worth trying before resetting the
    state database because many `quick_check` failures are in pages that do not
    prevent a normal schema/data dump.
    """
    dump = subprocess.run(
        [str(sqlite3_bin), "-batch", str(source_db), ".dump"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sql_file.write_text(dump.stdout, encoding="utf-8", errors="replace")
    (sql_file.with_suffix(sql_file.suffix + ".stderr.txt")).write_text(
        dump.stderr, encoding="utf-8", errors="replace"
    )
    if not dump.stdout.strip():
        return (False, f"sqlite3 .dump produced no SQL; stderr: {dump.stderr.strip()[:500]}")
    if "ROLLBACK; -- due to errors" in dump.stdout:
        return (False, f"sqlite3 .dump hit corruption before completion; stderr: {dump.stderr.strip()[:500]}")

    return _load_recovery_sql(sqlite3_bin, recovered_db, dump.stdout, "dump")


def cmd_db_health(args: argparse.Namespace) -> int:
    paths = resolve_codex_paths(args)
    con.header(f"codex-repair v{SCRIPT_VERSION} :: db-health")
    con.info(f"CODEX_HOME  = {paths.codex_home}")
    con.info(f"SQLite home = {paths.sqlite_home} (source: {paths.sqlite_home_source})")
    rc = EXIT_HEALTHY
    for spec in DB_SPECS:
        db = paths.db_path(spec)
        con.section(db.name)
        if not db.exists():
            if spec.required:
                con.err(f"missing database: {db}")
                rc = EXIT_NO_DB
            else:
                con.info(f"optional database missing: {db}")
            continue
        con.info(f"path = {db}")
        con.info(f"size = {db.stat().st_size:,} bytes")
        for side in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if side.exists():
                con.info(f"sidecar = {side.name} ({side.stat().st_size:,} bytes)")
        ok, msg = sqlite_check(db, full=getattr(args, "full", False))
        if ok:
            con.ok(("integrity_check" if getattr(args, "full", False) else "quick_check") + " = ok")
        else:
            con.err(("integrity_check" if getattr(args, "full", False) else "quick_check") + f" failed: {msg}")
            rc = EXIT_ERROR
    return rc


def cmd_recover_state_db(args: argparse.Namespace) -> int:
    paths = resolve_codex_paths(args)
    state_db = paths.state_db
    con.header(f"codex-repair v{SCRIPT_VERSION} :: recover-state-db")
    con.info(f"CODEX_HOME  = {paths.codex_home}")
    con.info(f"SQLite home = {paths.sqlite_home} (source: {paths.sqlite_home_source})")
    con.info("Close Codex before using --apply. This command never touches session JSONL files.")
    if not state_db.exists():
        con.err(f"missing {state_db}")
        return EXIT_NO_DB

    sqlite3_bins = _sqlite3_candidates(getattr(args, "sqlite3", None))
    if not sqlite3_bins:
        con.err("sqlite3 command-line tool not found")
        con.info("fallback reset: python3 codex-repair.py reset-state-db --apply")
        return EXIT_ERROR

    repair_dir = _timestamped_repair_dir(paths.sqlite_home, "state_5-recover" if args.apply else "state_5-recover-dryrun")
    con.info(f"repair directory = {repair_dir}")
    _copy_existing(_state_db_sidecars(state_db), repair_dir)
    con.ok("copied current state DB and sidecars into repair directory")

    con.section("sqlite3 candidates")
    recover_capable: list[Path] = []
    for bin_path in sqlite3_bins:
        version = _sqlite3_version(bin_path)
        supports = _sqlite3_supports_recover(bin_path)
        con.info(f"{bin_path} :: {version} :: .recover={'yes' if supports else 'no'}")
        if supports:
            recover_capable.append(bin_path)

    recovered_db: Optional[Path] = None
    success_msg: Optional[str] = None

    if recover_capable:
        con.section("Trying sqlite3 .recover")
        for i, bin_path in enumerate(recover_capable, start=1):
            candidate_db = repair_dir / f"state_5.recovered.recover-{i}.sqlite"
            sql_file = repair_dir / f"state_5.recover-{i}.sql"
            ok, msg = _run_sqlite_recover(bin_path, state_db, candidate_db, sql_file)
            if ok:
                recovered_db = candidate_db
                success_msg = f".recover succeeded using {bin_path}"
                break
            con.warn(f".recover using {bin_path} failed: {msg}")
    else:
        con.warn("none of the detected sqlite3 shells support .recover")

    if recovered_db is None:
        con.section("Trying sqlite3 .dump fallback")
        for i, bin_path in enumerate(sqlite3_bins, start=1):
            candidate_db = repair_dir / f"state_5.recovered.dump-{i}.sqlite"
            sql_file = repair_dir / f"state_5.dump-{i}.sql"
            ok, msg = _run_sqlite_dump_salvage(bin_path, state_db, candidate_db, sql_file)
            if ok:
                recovered_db = candidate_db
                success_msg = f".dump fallback succeeded using {bin_path}"
                break
            con.warn(f".dump using {bin_path} failed: {msg}")

    if recovered_db is None:
        con.err("could not create a healthy recovered state_5.sqlite")
        con.info(f"recovery SQL/logs are in {repair_dir}")
        con.info("next safe fallback: python3 codex-repair.py reset-state-db --apply")
        con.info("optional: install a modern SQLite shell, then rerun recovery; e.g. `brew install sqlite`")
        return EXIT_ERROR

    con.ok(f"{success_msg}; recovered DB quick_check = ok ({recovered_db})")

    if not args.apply:
        con.warn("dry-run only — original state_5.sqlite was not replaced")
        con.info("to replace it with the recovered copy, rerun with: recover-state-db --apply")
        return EXIT_HEALTHY

    # Move the current DB aside and install the recovered copy.
    _move_existing(_state_db_sidecars(state_db), repair_dir, suffix=".replaced")
    shutil.copy2(recovered_db, state_db)
    con.ok(f"installed recovered DB as {state_db}")
    con.info(f"the previous DB and sidecars were moved to {repair_dir}")
    return EXIT_HEALTHY


def cmd_reset_state_db(args: argparse.Namespace) -> int:
    paths = resolve_codex_paths(args)
    state_db = paths.state_db
    con.header(f"codex-repair v{SCRIPT_VERSION} :: reset-state-db")
    con.info(f"CODEX_HOME  = {paths.codex_home}")
    con.info(f"SQLite home = {paths.sqlite_home} (source: {paths.sqlite_home_source})")
    con.warn("This moves state_5.sqlite aside so Codex can recreate it on next launch.")
    con.info("It does not delete ~/.codex/sessions or ~/.codex/archived_sessions JSONL files.")
    existing = [p for p in _state_db_sidecars(state_db) if p.exists()]
    if not existing:
        con.ok("no state_5.sqlite files exist; nothing to reset")
        return EXIT_HEALTHY
    repair_dir = paths.sqlite_home / f"state_5-reset-{time.strftime('%Y%m%d-%H%M%S')}"
    if not args.apply:
        con.info(f"would create {repair_dir}")
        for p in existing:
            con.info(f"would move {p.name} -> {repair_dir}/{p.name}.reset")
        con.warn("dry-run only — pass --apply to actually move the DB aside")
        return EXIT_HEALTHY
    repair_dir.mkdir(parents=True, exist_ok=False)
    _move_existing(existing, repair_dir, suffix=".reset")
    con.ok(f"moved {len(existing)} file(s) into {repair_dir}")
    con.info("Now launch Codex once so it recreates state_5.sqlite, then quit Codex and rerun doctor/manual-backfill if needed.")
    return EXIT_HEALTHY

# ---------------------------------------------------------------------------
# Subcommand: doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = resolve_codex_paths(args)
    explicit_binary = Path(args.binary).expanduser().resolve() if args.binary else None
    binary = explicit_binary if explicit_binary else find_backend_binary(paths.codex_home)

    con.header(f"codex-repair v{SCRIPT_VERSION} :: doctor")
    con.section("Environment")
    con.info(f"CODEX_HOME  = {paths.codex_home}")
    con.info(f"SQLite home = {paths.sqlite_home} (source: {paths.sqlite_home_source})")
    con.info(f"Sessions    = {paths.sessions_dir}")
    emit_wsl_sqlite_layout_note(paths)

    missing_db = False
    for spec in DB_SPECS:
        db = paths.db_path(spec)
        if not db.exists():
            if spec.required:
                con.err(f"missing database: {db}")
                missing_db = True
            else:
                con.info(f"optional database missing: {db.name}")
        else:
            con.info(f"db         = {db.name} ({db.stat().st_size:,} bytes)")
    if missing_db:
        return EXIT_NO_DB

    # If using isolated copies for safety, copy them now.
    if args.use_isolated_copy:
        cache = Path(tempfile.mkdtemp(prefix="codex-repair-doctor-"))
        con.section(f"Isolated copy mode → {cache}")
        for db in paths.existing_db_paths():
            isolate_copy(db, cache)
        paths = dataclasses.replace(
            paths,
            sqlite_home=cache,
            sqlite_home_input=cache,
            sqlite_home_source=f"{paths.sqlite_home_source}+isolated",
        )
        con.ok("copied DBs to isolated location; original DB is untouched")

    con.section("SQLite health")
    db_malformed = False
    for db in paths.existing_db_paths():
        ok, msg = sqlite_check(db, full=False)
        if ok:
            con.ok(f"{db.name}: quick_check ok")
        else:
            con.err(f"{db.name}: quick_check failed: {msg}")
            db_malformed = True

    binary_ok = False
    anchors: list[BinaryAnchor] = []
    if binary:
        con.section("Backend binary")
        con.info(f"backend    = {binary}")
        st = binary.stat()
        con.info(f"             ({st.st_size:,} bytes, modified {time.ctime(st.st_mtime)})")
        if not is_native_executable(binary):
            con.err("selected backend is not a native executable")
            con.warn("it looks like a Node/npm launcher; checksum scanning would be meaningless")
        elif not db_malformed:
            con.section("Scanning binary for migration anchors")
            t0 = time.time()
            descriptions = _collect_all_descriptions(*paths.existing_db_paths())
            con.debug(f"  using {len(descriptions)} known descriptions as locators")
            anchors = scan_binary_anchors(binary, descriptions_hint=descriptions)
            con.ok(f"found {len(anchors)} (sql, sha384) anchors in {time.time()-t0:.1f}s")
            if anchors:
                binary_ok = True
            else:
                con.err("no migration anchors found in selected backend")
                con.warn("this usually means the selected file is a launcher/wrapper, not the native Rust Codex binary")
        else:
            con.warn("skipping binary checksum scan because a DB failed quick_check")
    else:
        con.section("Backend binary")
        con.warn("no native backend binary found — checksum drift checks will be skipped")

    overall_exit = EXIT_HEALTHY
    if db_malformed:
        con.section("Checksum drift check")
        con.warn("skipped because at least one database failed SQLite quick_check")
    elif binary_ok:
        all_diffs: list[ChecksumDiff] = []
        for spec in DB_SPECS:
            db = paths.db_path(spec)
            if not db.exists():
                continue
            con.section(f"Checksum drift check :: {db.name}")
            diffs = compute_checksum_diffs(db, binary, anchors)
            if (
                not spec.required
                and diffs
                and all(d.binary_anchor is None for d in diffs)
            ):
                con.warn(f"skipping optional {db.name}: no matching migration anchors found in selected backend")
                continue
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
    else:
        con.section("Checksum drift check")
        con.warn("skipped because no usable native backend binary was found")
        overall_exit = EXIT_NO_BINARY

    con.section("Backfill state")
    db_read_error: Optional[sqlite3.Error] = None
    bf: Optional[BackfillStatus] = None
    if db_malformed:
        con.warn("skipped because state_5.sqlite failed quick_check")
    else:
        try:
            bf = collect_backfill_status(paths.codex_home, paths.state_db)
        except sqlite3.Error as exc:
            con.err(f"could not read backfill_state: {exc}")
            con.warn("state_5.sqlite could be corrupt or being read while Codex is mutating it")
            db_read_error = exc
    if bf:
        con.info(f"status              = {bf.status}")
        con.info(f"last_watermark      = {bf.last_watermark}")
        con.info(f"indexed threads     = {bf.indexed_threads}")
        con.info(f"sessions jsonl      = {bf.sessions_jsonl_count}")
        con.info(f"archived jsonl      = {bf.archived_jsonl_count}")
        con.info(f"unindexed indexable = {len(bf.unindexed_files)}")
        con.info(f"ignored unindexable = {len(bf.ignored_unindexable_files)}")
        if bf.ignored_unindexable_files:
            for p, reason in bf.ignored_unindexable_files[:5]:
                con.info(f"  ignored {p.name}: {reason}")
            if len(bf.ignored_unindexable_files) > 5:
                con.info(f"  ... {len(bf.ignored_unindexable_files) - 5} more ignored")
        if bf.is_stuck:
            con.warn("backfill appears stuck (status incomplete)")
            con.warn("→ on next Codex launch, 30s startup timeout may fire")
            if overall_exit == EXIT_HEALTHY:
                overall_exit = EXIT_BACKFILL_STUCK
            elif overall_exit == EXIT_CHECKSUM_DRIFT:
                overall_exit = EXIT_BOTH
        else:
            if bf.unindexed_files:
                con.warn(
                    "backfill is marked complete; valid unindexed JSONL files may mean "
                    "thread metadata is stale, but startup should not block"
                )
            if bf.ignored_unindexable_files:
                con.ok("backfill complete; remaining unindexed JSONL files are non-session/legacy files")
            elif not bf.unindexed_files:
                con.ok("backfill complete, no unindexed files")

    con.section("Summary")
    if db_malformed or db_read_error is not None:
        con.warn("detected: database read error / possible SQLite corruption")
        con.info("do not run `fix --apply` until this is resolved")
        con.info("recommended: python codex-repair.py db-health")
        con.info("try recovery: python3 codex-repair.py recover-state-db --apply")
        con.info("fallback reset: python3 codex-repair.py reset-state-db --apply")
        return EXIT_ERROR
    if overall_exit == EXIT_HEALTHY:
        con.ok("install is healthy — no action needed")
    elif overall_exit == EXIT_NO_BINARY:
        con.warn("detected: no usable native backend binary, so checksum checks were skipped")
        con.info("database health/backfill checks above are still valid")
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
    paths = resolve_codex_paths(args)
    binary = Path(args.binary).resolve() if args.binary else find_backend_binary(paths.codex_home)
    if not binary:
        con.err("no backend binary found")
        return EXIT_NO_BINARY

    con.header(f"codex-repair v{SCRIPT_VERSION} :: fix-checksums")
    con.info(f"CODEX_HOME  = {paths.codex_home}")
    con.info(f"SQLite home = {paths.sqlite_home} (source: {paths.sqlite_home_source})")

    con.section("Scanning binary for expected checksums...")
    descriptions = _collect_all_descriptions(*paths.existing_db_paths())
    anchors = scan_binary_anchors(binary, descriptions_hint=descriptions)
    con.ok(f"found {len(anchors)} anchors")
    if not anchors:
        con.err("no migration anchors found in selected backend")
        con.warn("refusing to compare or rewrite checksums against a launcher/wrapper")
        return EXIT_NO_BINARY

    if args.use_isolated_copy:
        cache = Path(tempfile.mkdtemp(prefix="codex-repair-fix-"))
        con.section(f"Isolated copy mode → {cache}")
        for db in paths.existing_db_paths():
            isolate_copy(db, cache)
        paths = dataclasses.replace(
            paths,
            sqlite_home=cache,
            sqlite_home_input=cache,
            sqlite_home_source=f"{paths.sqlite_home_source}+isolated",
        )
        con.ok("real DBs untouched; operating on isolated copies (forces dry-run)")
        args.apply = False

    fixes_planned: list[ChecksumDiff] = []
    for spec in DB_SPECS:
        db = paths.db_path(spec)
        if not db.exists():
            if spec.required:
                con.err(f"missing database: {db}")
                return EXIT_NO_DB
            con.info(f"optional database missing: {db.name}")
            continue
        con.section(f"Checking {db.name}")
        diffs = compute_checksum_diffs(db, binary, anchors)
        if (
            not spec.required
            and diffs
            and all(d.binary_anchor is None for d in diffs)
        ):
            con.warn(f"skipping optional {db.name}: no matching migration anchors found in selected backend")
            continue
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
    paths = resolve_codex_paths(args)
    state_db = paths.state_db

    con.header(f"codex-repair v{SCRIPT_VERSION} :: manual-backfill")
    con.info(f"CODEX_HOME  = {paths.codex_home}")
    con.info(f"SQLite home = {paths.sqlite_home} (source: {paths.sqlite_home_source})")
    con.info(f"Sessions    = {paths.sessions_dir}")
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

    sessions_dir = paths.sessions_dir
    archived_dir = paths.archived_sessions_dir
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
                and path_is_relative_to(win_path, archived_root)
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
        if not planned_inserts:
            con.ok("no indexable unindexed session files remain; skipped files are non-session/legacy JSONL")

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
                rel = newest.relative_to(paths.codex_home).as_posix()
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
# Subcommand: quarantine-invalid-jsonl
# ---------------------------------------------------------------------------


def cmd_quarantine_invalid_jsonl(args: argparse.Namespace) -> int:
    """Move unindexed, non-session JSONL files out of sessions/.

    This only moves files that are not already referenced by `threads.rollout_path`
    and that cannot be indexed because the first line is not a usable
    `session_meta` record. It never deletes files.
    """
    paths = resolve_codex_paths(args)
    state_db = paths.state_db
    con.header(f"codex-repair v{SCRIPT_VERSION} :: quarantine-invalid-jsonl")
    con.info(f"CODEX_HOME  = {paths.codex_home}")
    con.info(f"SQLite home = {paths.sqlite_home} (source: {paths.sqlite_home_source})")
    if not state_db.exists():
        con.err(f"missing {state_db}")
        return EXIT_NO_DB

    ok, msg = sqlite_check(state_db, full=False)
    if not ok:
        con.err(f"state_5.sqlite quick_check failed: {msg}")
        con.warn("not moving JSONL files while the state DB is unhealthy")
        return EXIT_ERROR

    indexed = read_indexed_rollout_paths(state_db)
    scheme = detect_rollout_path_scheme(state_db)
    all_files, _sessions_dir, _archived_dir = collect_jsonl_files(paths.codex_home)

    candidates: list[tuple[Path, str]] = []
    for fp in all_files:
        rp = windows_path_to_rollout(fp, scheme)
        if rp in indexed:
            continue
        is_indexable, reason = classify_session_jsonl(fp)
        if not is_indexable:
            candidates.append((fp, reason))

    con.info(f"unindexed non-session/legacy JSONL files = {len(candidates)}")
    if not candidates:
        con.ok("nothing to quarantine")
        return EXIT_HEALTHY

    for p, reason in candidates[:10]:
        display = p.relative_to(paths.codex_home) if path_is_relative_to(p, paths.codex_home) else p
        con.info(f"  {display}: {reason}")
    if len(candidates) > 10:
        con.info(f"  ... {len(candidates)-10} more")

    if not args.apply:
        con.warn("dry-run only — pass --apply to move these files to a quarantine directory")
        return EXIT_HEALTHY

    ts = time.strftime("%Y%m%d-%H%M%S")
    quarantine_dir = paths.codex_home / f"invalid-jsonl-quarantine-{ts}"
    manifest: list[dict[str, str]] = []
    for src, reason in candidates:
        rel = src.relative_to(paths.codex_home) if path_is_relative_to(src, paths.codex_home) else Path(src.name)
        dest = quarantine_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        manifest.append({"from": str(src), "to": str(dest), "reason": reason})
    manifest_path = quarantine_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    con.ok(f"moved {len(candidates)} file(s) to {quarantine_dir}")
    con.ok(f"wrote manifest: {manifest_path}")
    return EXIT_HEALTHY


# ---------------------------------------------------------------------------
# Subcommand: extract-checksums
# ---------------------------------------------------------------------------


def cmd_extract_checksums(args: argparse.Namespace) -> int:
    paths = resolve_codex_paths(args)
    binary = Path(args.binary).resolve() if args.binary else find_backend_binary(paths.codex_home)
    if not binary:
        con.err("no backend binary found")
        return EXIT_NO_BINARY

    con.header(f"codex-repair v{SCRIPT_VERSION} :: extract-checksums")
    con.info(f"binary = {binary}")
    # Use DB descriptions as locator if DBs exist; otherwise fallback algorithm runs.
    descriptions = _collect_all_descriptions(*paths.existing_db_paths())
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
    if rc not in (EXIT_CHECKSUM_DRIFT, EXIT_BACKFILL_STUCK, EXIT_BOTH):
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


def _add_global_flags(p: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """Add the global flags to a (sub)parser so they work in any position."""
    default_codex_home = argparse.SUPPRESS if suppress_defaults else str(DEFAULT_CODEX_HOME)
    default_none = argparse.SUPPRESS if suppress_defaults else None
    default_false = argparse.SUPPRESS if suppress_defaults else False
    p.add_argument(
        "--codex-home",
        default=default_codex_home,
        help=f"Codex home directory (default: {DEFAULT_CODEX_HOME})",
    )
    p.add_argument(
        "--sqlite-home",
        default=default_none,
        help=(
            "SQLite state directory (default: config sqlite_home, then "
            "CODEX_SQLITE_HOME, then CODEX_HOME/sqlite when present, then CODEX_HOME)"
        ),
    )
    p.add_argument(
        "--binary",
        default=default_none,
        help="Backend binary path (default: auto-detect native Codex backend, including npm macOS wrappers)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=default_false,
        help="Actually modify databases (default: dry-run)",
    )
    p.add_argument(
        "--use-isolated-copy",
        action="store_true",
        default=default_false,
        help="Copy DBs to a temp dir and operate on copies (zero risk to running Codex; implies dry-run)",
    )
    p.add_argument(
        "--sqlite3",
        default=default_none,
        help="sqlite3 shell to use for recover-state-db (default: auto-detect Homebrew/MacPorts/PATH)",
    )
    p.add_argument("-v", "--verbose", action="store_true", default=default_false)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-repair",
        description="Unified repair tool for Codex Desktop (macOS, Windows, and WSL backend).",
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
        ("quarantine-invalid-jsonl", "Move unindexed non-session JSONL files out of sessions/"),
        ("recover-state-db", "Try SQLite .recover/.dump for state_5.sqlite (dry-run unless --apply)"),
        ("reset-state-db", "Move state_5.sqlite aside so Codex can recreate it (dry-run unless --apply)"),
    ):
        sp = sub.add_parser(name, help=help_text)
        _add_global_flags(sp, suppress_defaults=True)
    dbh = sub.add_parser("db-health", help="Check SQLite DB health without needing a backend binary")
    _add_global_flags(dbh, suppress_defaults=True)
    dbh.add_argument("--full", action="store_true", help="run PRAGMA integrity_check instead of quick_check (slower)")
    extract = sub.add_parser("extract-checksums", help="List all migration checksums from binary")
    _add_global_flags(extract, suppress_defaults=True)
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
        ("sqlite_home", None),
        ("binary", None),
        ("apply", False),
        ("use_isolated_copy", False),
        ("sqlite3", None),
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
        "quarantine-invalid-jsonl": cmd_quarantine_invalid_jsonl,
        "db-health": cmd_db_health,
        "recover-state-db": cmd_recover_state_db,
        "reset-state-db": cmd_reset_state_db,
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
