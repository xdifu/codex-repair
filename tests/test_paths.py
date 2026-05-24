import argparse
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_repair", ROOT / "codex-repair.py")
codex_repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = codex_repair
SPEC.loader.exec_module(codex_repair)


def args(codex_home: Path, sqlite_home: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(codex_home=str(codex_home), sqlite_home=str(sqlite_home) if sqlite_home else None)


class PathResolutionTests(unittest.TestCase):
    def test_cli_sqlite_home_has_highest_priority(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": "/env/sqlite"}):
            root = Path(td)
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('sqlite_home = "/config/sqlite"\n', encoding="utf-8")
            (codex_home / "sqlite").mkdir()
            (codex_home / "sqlite" / codex_repair.STATE_DB_NAME).touch()

            paths = codex_repair.resolve_codex_paths(args(codex_home, root / "cli-sqlite"))

            self.assertEqual(paths.sqlite_home_source, "cli")
            self.assertEqual(paths.sqlite_home, (root / "cli-sqlite").resolve())

    def test_config_sqlite_home_beats_env(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": "/env/sqlite"}):
            root = Path(td)
            codex_home = root / "codex"
            codex_home.mkdir()
            config_sqlite = root / "config-sqlite"
            (codex_home / "config.toml").write_text(f'sqlite_home = "{config_sqlite}"\n', encoding="utf-8")

            paths = codex_repair.resolve_codex_paths(args(codex_home))

            self.assertEqual(paths.sqlite_home_source, "config")
            self.assertEqual(paths.sqlite_home, config_sqlite.resolve())

    def test_env_sqlite_home_beats_legacy_sqlite_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": "/env/sqlite"}):
            root = Path(td)
            codex_home = root / "codex"
            legacy = codex_home / "sqlite"
            legacy.mkdir(parents=True)
            (legacy / codex_repair.STATE_DB_NAME).touch()

            paths = codex_repair.resolve_codex_paths(args(codex_home))

            self.assertEqual(paths.sqlite_home_source, "env")
            self.assertEqual(paths.sqlite_home, Path("/env/sqlite").resolve(strict=False))

    def test_legacy_sqlite_subdir_requires_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(td)
            codex_home = root / "codex"
            (codex_home / "sqlite").mkdir(parents=True)

            paths = codex_repair.resolve_codex_paths(args(codex_home))

            self.assertEqual(paths.sqlite_home_source, "default")
            self.assertEqual(paths.sqlite_home, codex_home.resolve())

            (codex_home / "sqlite" / codex_repair.STATE_DB_NAME).touch()
            paths = codex_repair.resolve_codex_paths(args(codex_home))
            self.assertEqual(paths.sqlite_home_source, "legacy")
            self.assertEqual(paths.sqlite_home, (codex_home / "sqlite").resolve())

    def test_fallback_sqlite_home_is_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {}, clear=True):
            codex_home = Path(td) / "codex"
            codex_home.mkdir()

            paths = codex_repair.resolve_codex_paths(args(codex_home))

            self.assertEqual(paths.sqlite_home_source, "default")
            self.assertEqual(paths.sqlite_home, codex_home.resolve())


class WslLayoutTests(unittest.TestCase):
    def test_wsl_mnt_sqlite_is_risk(self) -> None:
        paths = codex_repair.CodexPaths(
            codex_home=Path("/mnt/c/Users/me/.codex"),
            sqlite_home=Path("/mnt/c/Users/me/.codex"),
            sqlite_home_source="default",
            codex_home_input=Path("/mnt/c/Users/me/.codex"),
            sqlite_home_input=Path("/mnt/c/Users/me/.codex"),
        )
        with mock.patch.object(codex_repair, "is_wsl", return_value=True):
            self.assertEqual(codex_repair.wsl_sqlite_layout_status(paths), "risk")

    def test_wsl_mnt_codex_home_with_native_sqlite_is_split(self) -> None:
        paths = codex_repair.CodexPaths(
            codex_home=Path("/mnt/c/Users/me/.codex"),
            sqlite_home=Path("/home/me/.codex-sqlite"),
            sqlite_home_source="env",
            codex_home_input=Path("/mnt/c/Users/me/.codex"),
            sqlite_home_input=Path("/home/me/.codex-sqlite"),
        )
        with mock.patch.object(codex_repair, "is_wsl", return_value=True):
            self.assertEqual(codex_repair.wsl_sqlite_layout_status(paths), "split")


class BackfillStatusTests(unittest.TestCase):
    def test_complete_backfill_with_unindexed_files_is_not_startup_stuck(self) -> None:
        bf = codex_repair.BackfillStatus(
            status="complete",
            last_watermark=None,
            last_success_at=None,
            indexed_threads=1,
            sessions_jsonl_count=2,
            archived_jsonl_count=0,
            unindexed_files=[Path("/tmp/rollout.jsonl")],
            ignored_unindexable_files=[],
        )

        self.assertFalse(bf.is_stuck)


if __name__ == "__main__":
    unittest.main()
