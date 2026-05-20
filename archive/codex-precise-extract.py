"""Re-extract migration 1 SQL with PRECISE boundary and verify checksum."""
import mmap
import hashlib

target = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

with open(target, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    sql_start = 175746441           # offset of "CREATE TABLE threads"
    checksum_start = 175747304      # offset of 627EF191 (48-byte hash)

    sql_bytes = data[sql_start:checksum_start]
    expected_checksum_bytes = data[checksum_start:checksum_start+48]

    print(f"SQL length:        {len(sql_bytes)} bytes")
    print(f"Expected checksum: {expected_checksum_bytes.hex().upper()}")
    print()
    print("=== migration 1 SQL ===")
    print(sql_bytes.decode("utf-8", errors="replace"))
    print("=== END SQL ===")
    print()

    # Compute SHA-384 of the SQL (sqlx default)
    h = hashlib.sha384(sql_bytes).hexdigest().upper()
    print(f"SHA384(sql_bytes)              = {h}")
    print(f"Expected hardcoded checksum    = {expected_checksum_bytes.hex().upper()}")
    print(f"Match? {h == expected_checksum_bytes.hex().upper()}")
    print()

    # Maybe SQL ends with trailing newline that we cut off
    # Try variants
    for label, s in [
        ("sql + \\n",     sql_bytes + b"\n"),
        ("sql + ;",       sql_bytes + b";"),
        ("sql rstrip",    sql_bytes.rstrip()),
        ("sql strip",     sql_bytes.strip()),
        ("sql w/o trailing extra null", sql_bytes.rstrip(b"\x00")),
    ]:
        h2 = hashlib.sha384(s).hexdigest().upper()
        match = "MATCH!" if h2 == expected_checksum_bytes.hex().upper() else ""
        print(f"  SHA384({label:30}) = {h2[:48]}...  {match}")

    print()
    # Also check what is stored in fresh DB
    print("=== Compare with DB record ===")
    import sqlite3
    dbpath = r"C:\Users\Xiao Difu\.codex\state_5.sqlite"
    import os
    if os.path.exists(dbpath):
        con = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT version, description, hex(checksum) FROM _sqlx_migrations WHERE version=1")
        row = cur.fetchone()
        if row:
            print(f"  DB stored checksum:    {row[2]}")
            print(f"  Binary hardcoded:      {expected_checksum_bytes.hex().upper()}")
            print(f"  Match between them? {row[2].upper() == expected_checksum_bytes.hex().upper()}")
        con.close()

    data.close()
