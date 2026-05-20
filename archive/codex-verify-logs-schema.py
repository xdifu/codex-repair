"""Verify logs_2.sqlite actual schema matches the NEW migration SQL expectations."""
import sqlite3
import mmap
import hashlib

logs_db = r"C:\Users\Xiao Difu\.codex\logs_2.sqlite"
new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

# Show actual schema
print("=" * 70)
print("Actual schema in logs_2.sqlite:")
print("=" * 70)
con = sqlite3.connect(f"file:{logs_db}?mode=ro", uri=True)
cur = con.cursor()
cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
for row in cur.fetchall():
    print(f"\nTable: {row[0]}")
    print(row[1])
print()

cur.execute("PRAGMA table_info(logs)")
print("logs columns (PRAGMA):")
for c in cur.fetchall():
    print(f"  {c}")

cur.execute("SELECT COUNT(*) FROM logs")
print(f"\nlogs row count: {cur.fetchone()[0]}")

cur.execute("SELECT version, description, hex(checksum) FROM _sqlx_migrations ORDER BY version")
print("\nCurrent migration records:")
db_migs = cur.fetchall()
for m in db_migs:
    print(f"  m{m[0]} desc={m[1]!r}  cksum={m[2]}")
con.close()
print()

# Extract all migration SQL/checksum pairs from binary near offset 175744000
# logs_2 migrations should be sequential right there
print("=" * 70)
print("Find logs_2 migration sequence in binary:")
print("=" * 70)
with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    # Region of interest is around offset 175744000-175747000.
    # We already located m1 at 175744478 with SQL length 730.
    # m2 description "logs feedback log body" - try to find it.
    p_m2_desc = data.find(b"logs feedback log body")
    print(f"  'logs feedback log body' string offset: {p_m2_desc}")
    # m2 SQL starts right after that description
    p_m2_sql = data.find(b"CREATE TABLE logs", 175745000)
    print(f"  m2 CREATE TABLE logs offset: {p_m2_sql}")
    # Find hash anchor for m2
    for L in range(200, 5000):
        sql_try = data[p_m2_sql:p_m2_sql+L]
        actual = data[p_m2_sql+L:p_m2_sql+L+48]
        if hashlib.sha384(sql_try).digest() == actual:
            print(f"\n  m2: SQL length={L}, checksum={data[p_m2_sql+L:p_m2_sql+L+48].hex().upper()}")
            print(f"  m2 SQL:")
            print(data[p_m2_sql:p_m2_sql+L].decode("utf-8", errors="replace"))
            break
    data.close()

print()
print("=" * 70)
print("SUMMARY:")
print("=" * 70)
print(f"  DB m1 stored checksum:    {db_migs[0][2]}")
print(f"  Binary m1 expected:       009639EAFE599BE97D49D1D712E51671BF1BE1C6B8CB7BF1A4136DA88FB0E19308FF3CBCFBFBF589065E0EAFEF2CA164")
print(f"  DB m2 stored checksum:    {db_migs[1][2]}")
