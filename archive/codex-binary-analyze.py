"""Read codex Linux ELF and search for SQL migration text + flags.
Does NOT touch any data files."""
import mmap
import re
import hashlib

target = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

with open(target, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    print("=" * 70)
    print("1. Search for migration 1 SQL ('CREATE TABLE threads' family)")
    print("=" * 70)
    for needle in [
        b"CREATE TABLE IF NOT EXISTS threads",
        b"CREATE TABLE threads",
        b"-- threads",
        b"_sqlx_migrations",
    ]:
        positions = []
        start = 0
        while True:
            pos = data.find(needle, start)
            if pos == -1:
                break
            positions.append(pos)
            start = pos + 1
            if len(positions) > 5:
                break
        print(f"  needle={needle!r}: {len(positions)} hits at {positions[:5]}")

    print()
    print("=" * 70)
    print("2. Print first SQL block found")
    print("=" * 70)
    p = data.find(b"CREATE TABLE")
    if p != -1:
        # Find a reasonable boundary by searching for next null byte cluster
        chunk = data[max(p-100, 0):p+3000]
        # Strip after first run of null bytes
        cut = chunk.find(b"\x00\x00\x00")
        if cut > 0:
            chunk = chunk[:cut]
        try:
            txt = chunk.decode("utf-8", errors="replace")
            print(txt)
        except Exception as e:
            print(f"decode err: {e}")

    print()
    print("=" * 70)
    print("3. Search for migration-validation flags / env vars")
    print("=" * 70)
    for needle in [
        b"SQLX_IGNORE_",
        b"IGNORE_MIGRATION",
        b"MIGRATION_MISMATCH",
        b"CODEX_ALLOW",
        b"CODEX_SKIP",
        b"CODEX_FORCE",
        b"CODEX_RESET",
        b"--reset-state",
        b"--skip-migration",
        b"--ignore-migration",
        b"--force",
        b"reset-state-db",
        b"reset_state_db",
        b"was previously applied",
        b"has been modified",
    ]:
        positions = []
        start = 0
        while True:
            pos = data.find(needle, start)
            if pos == -1:
                break
            positions.append(pos)
            start = pos + 1
            if len(positions) > 3:
                break
        if positions:
            sample = data[max(positions[0]-40,0):positions[0]+200]
            sample_text = sample.decode("utf-8", errors="replace")
            print(f"  {needle!r}: {len(positions)} hits")
            print(f"     ctx: ...{sample_text!r}...")

    print()
    print("=" * 70)
    print("4. Find ALL occurrences of '_sqlx_migrations' context")
    print("=" * 70)
    start = 0
    cnt = 0
    while cnt < 10:
        pos = data.find(b"_sqlx_migrations", start)
        if pos == -1:
            break
        cnt += 1
        sample = data[max(pos-50,0):pos+250]
        txt = sample.decode("utf-8", errors="replace")
        print(f"  @{pos}: {txt!r}")
        start = pos + 1

    print()
    print("=" * 70)
    print("5. Search for the migration name 'threads' near hash-like contexts")
    print("=" * 70)
    # In sqlx, migrate! embeds: version: i64, description: &str, sql: &str, checksum: [u8; N]
    # Search for the literal description "threads" preceded by length-prefix patterns

    # Just print first 5 occurrences of " threads" as a string boundary
    start = 0
    cnt = 0
    while cnt < 3:
        pos = data.find(b"threads", start)
        if pos == -1:
            break
        cnt += 1
        # Look backwards for a likely SQL begin
        backctx = data[max(pos-300,0):pos+50]
        txt = backctx.decode("utf-8", errors="replace")
        print(f"  @{pos}: ...{txt!r}")
        start = pos + 1

    data.close()
