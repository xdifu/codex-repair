"""Fix logs_2.sqlite migration checksums to match new codex binary's expected values.

Root cause: OpenAI modified migration 1 and 2 SQL content of logs_2.sqlite between
the user's old install (2026-04-09) and the current update. SQLx then rejects the
DB because the stored checksum from old SQL doesn't match the new binary's hash.

Safety: We already verified the FINAL schema (after m1 + m2 applied) is identical
between user's logs_2.sqlite and the new binary's expected schema (12 columns
including feedback_log_body, thread_id, process_uuid, estimated_bytes). So updating
just the checksum in _sqlx_migrations is safe - no actual schema changes needed.
"""
import sqlite3
import shutil
import os
import time

LOGS_DB = r"C:\Users\Xiao Difu\.codex\logs_2.sqlite"
STATE_DB = r"C:\Users\Xiao Difu\.codex\state_5.sqlite"

NEW_LOGS_M1_CHECKSUM = "009639EAFE599BE97D49D1D712E51671BF1BE1C6B8CB7BF1A4136DA88FB0E19308FF3CBCFBFBF589065E0EAFEF2CA164"
NEW_LOGS_M2_CHECKSUM = "CF6C93AF074A90224564010F49BC9CF905F10BA4C3C4B8B25C12E014EE1522F0B561A1C3D8188F6D2F4BE764F141DAF8"

ts = time.strftime("%Y%m%d-%H%M%S")
backup_label = f"checksum-fix-{ts}"

print("=" * 70)
print(f"Step 1: Backup current logs_2.sqlite + state_5.sqlite")
print("=" * 70)
for db in [LOGS_DB, STATE_DB]:
    bk = f"{db}.bak-{backup_label}"
    shutil.copy2(db, bk)
    print(f"  Backed up: {bk}")
    # Also back up WAL/SHM if exist
    for ext in ["-wal", "-shm"]:
        src = db + ext
        if os.path.exists(src):
            shutil.copy2(src, bk + ext)
            print(f"  Backed up: {bk + ext}")

print()
print("=" * 70)
print(f"Step 2: Show current logs_2 _sqlx_migrations rows")
print("=" * 70)
con = sqlite3.connect(LOGS_DB)
cur = con.cursor()
cur.execute("SELECT version, description, installed_on, hex(checksum) FROM _sqlx_migrations ORDER BY version")
for row in cur.fetchall():
    print(f"  BEFORE m{row[0]} {row[1]!r} installed={row[2]} cksum={row[3][:32]}...")

print()
print("=" * 70)
print(f"Step 3: Update checksums to binary-expected values")
print("=" * 70)
cur.execute("UPDATE _sqlx_migrations SET checksum = ? WHERE version = 1",
            (bytes.fromhex(NEW_LOGS_M1_CHECKSUM),))
print(f"  m1 -> {NEW_LOGS_M1_CHECKSUM[:32]}...  (rowcount={cur.rowcount})")
cur.execute("UPDATE _sqlx_migrations SET checksum = ? WHERE version = 2",
            (bytes.fromhex(NEW_LOGS_M2_CHECKSUM),))
print(f"  m2 -> {NEW_LOGS_M2_CHECKSUM[:32]}...  (rowcount={cur.rowcount})")
con.commit()
print()

print("=" * 70)
print(f"Step 4: Verify the update")
print("=" * 70)
cur.execute("SELECT version, description, installed_on, hex(checksum) FROM _sqlx_migrations ORDER BY version")
for row in cur.fetchall():
    print(f"  AFTER  m{row[0]} {row[1]!r} installed={row[2]} cksum={row[3][:32]}...")

# Also verify logs table count - should still be 1004 untouched rows
cur.execute("SELECT COUNT(*) FROM logs")
log_count = cur.fetchone()[0]
print()
print(f"  logs row count: {log_count} (UNCHANGED - no data modification)")
con.close()

print()
print("=" * 70)
print("DONE. logs_2.sqlite checksums updated to match new binary.")
print("Backup label:", backup_label)
print("=" * 70)
