"""Find the EMBEDDED migration sequence for logs_2.sqlite in the new codex binary."""
import mmap
import hashlib
import sqlite3

new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"
logs_db = r"C:\Users\Xiao Difu\.codex\logs_2.sqlite"

# First, dump the DB-stored migrations for logs_2.sqlite
print("=" * 70)
print("logs_2.sqlite stored migrations:")
print("=" * 70)
con = sqlite3.connect(f"file:{logs_db}?mode=ro", uri=True)
cur = con.cursor()
cur.execute("SELECT version, description, installed_on, hex(checksum) FROM _sqlx_migrations ORDER BY version")
db_migs = cur.fetchall()
for row in db_migs:
    print(f"  m{row[0]}: desc={row[1]!r}  installed={row[2]}  cksum={row[3]}")
con.close()
print()

# Now scan codex binary for migration patterns.
# In sqlx::migrate! embedded form, we expect strings like:
#   <description>  ...  <SQL>  <48-byte checksum>
# We saw migration 1 = "threads" SQL @ offset X, followed by 48-byte SHA384, then "logs" description, etc.
# So the logs_2 migration set is in a separate location.

with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    # Search for all occurrences of "CREATE TABLE logs" (state DB and logs DB have one each)
    # In state_5 migration 2 = "logs" SQL.
    # logs_2 migration 1 also creates a different `logs` table (much different columns).

    print("=" * 70)
    print("All 'CREATE TABLE logs' occurrences:")
    print("=" * 70)
    start = 0
    positions = []
    while True:
        p = data.find(b"CREATE TABLE logs", start)
        if p == -1:
            break
        positions.append(p)
        start = p + 1
    for p in positions:
        ctx = data[p:p+500]
        print(f"\n  @{p}:")
        print(ctx.decode("utf-8", errors="replace"))

    # Also: in our prior search for the description strings, in state DB binary layout
    # we had: SQL bytes, then 48-byte checksum, then description of NEXT migration.
    # For the logs DB, we expect description 'logs' (same name) but DIFFERENT schema.
    # That database's m1 cksum should match what is stored if backend code matches DB record.

    # Brute force: For each CREATE TABLE logs occurrence, find the boundary L such that
    # SHA384(data[p:p+L]) == data[p+L:p+L+48]. That's the embedded checksum.
    print()
    print("=" * 70)
    print("Hash-anchor matching for each occurrence:")
    print("=" * 70)
    for p in positions:
        found_L = None
        for L in range(200, 4000):
            sql_try = data[p:p+L]
            actual = data[p+L:p+L+48]
            if hashlib.sha384(sql_try).digest() == actual:
                found_L = L
                break
        if found_L is None:
            print(f"  @{p}: NO hash anchor in range")
            continue
        cksum_hex = data[p+found_L:p+found_L+48].hex().upper()
        print(f"\n  @{p}: SQL length={found_L}, checksum={cksum_hex}")
        # Compare to DB-stored
        for row in db_migs:
            if row[3].upper() == cksum_hex:
                print(f"     >>> MATCHES logs_2.sqlite m{row[0]}={row[1]!r}  (already in DB)")
        # Show first few lines of SQL
        sql_text = data[p:p+found_L].decode("utf-8", errors="replace")
        # First 12 lines
        lines = sql_text.split("\n")[:15]
        print("     SQL preview:")
        for line in lines:
            print(f"       | {line}")

    data.close()

# Now also: what does the binary expect for logs_2 m1 ('logs')?
# We need to look up the EMBEDDED migration set list to find the 'logs' description
# that corresponds to logs_2 DB. They are likely in a separate part of the binary.
