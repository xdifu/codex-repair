"""Find ALL expected migration checksums from new codex binary, for both state_5 and logs_2 DBs."""
import mmap
import hashlib
import sqlite3

new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"
state_db  = r"C:\Users\Xiao Difu\.codex\state_5.sqlite"  # currently the new 180KB
logs_db   = r"C:\Users\Xiao Difu\.codex\logs_2.sqlite"

def fetch_migrations(db_path):
    rows = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT version, description, hex(checksum) FROM _sqlx_migrations ORDER BY version")
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        print(f"DB error {db_path}: {e}")
    return rows

print("=" * 70)
print("Current STATE_5 DB migrations:")
print("=" * 70)
state_migs = fetch_migrations(state_db)
for m in state_migs:
    print(f"  m{m[0]} {m[1]!r}: {m[2]}")

print()
print("=" * 70)
print("Current LOGS_2 DB migrations:")
print("=" * 70)
logs_migs = fetch_migrations(logs_db)
for m in logs_migs:
    print(f"  m{m[0]} {m[1]!r}: {m[2]}")

# Build a lookup of all valid migration (sql_len, checksum) pairs in the binary,
# by scanning for SHA-384 anchor matches.
# Strategy:
#   For each likely SQL start (CREATE TABLE / ALTER TABLE / DROP / UPDATE / INSERT etc),
#   try lengths L in 50..30000, compute SHA384(data[p:p+L]) and compare with data[p+L:p+L+48].

print()
print("=" * 70)
print("Scanning binary for ALL (SQL, checksum) anchors...")
print("=" * 70)

with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    # Restrict to data section neighborhood
    region_start = 175_700_000
    region_end   = 175_800_000

    # Candidate starting keywords
    starts = []
    for kw in [b"CREATE TABLE", b"ALTER TABLE", b"DROP TABLE", b"CREATE INDEX",
               b"DROP INDEX", b"UPDATE ", b"INSERT INTO", b"CREATE VIEW",
               b"WITH ", b"DELETE FROM"]:
        s = region_start
        while True:
            p = data.find(kw, s)
            if p == -1 or p > region_end:
                break
            starts.append(p)
            s = p + 1
    starts = sorted(set(starts))
    print(f"  {len(starts)} candidate SQL start offsets in region [{region_start}..{region_end}]")

    # For each start, try L in range and check hash anchor
    found = []
    for sp in starts:
        max_L = min(30000, region_end - sp - 48)
        for L in range(50, max_L):
            sql = data[sp:sp+L]
            actual = data[sp+L:sp+L+48]
            if hashlib.sha384(sql).digest() == actual:
                found.append((sp, L, actual.hex().upper()))
                break  # next start

    print(f"  Found {len(found)} hash anchors")
    for sp, L, cksum in found:
        # First line of SQL
        first_line = data[sp:sp+200].split(b"\n")[0].decode("utf-8", errors="replace")
        print(f"    @{sp}  L={L:5d}  cksum={cksum[:32]}...  | {first_line}")

    # Match each DB row to a binary anchor
    print()
    print("=" * 70)
    print("Match DB rows -> binary anchors:")
    print("=" * 70)
    bin_by_cksum = {h: (sp, L) for sp, L, h in found}

    print()
    print("STATE_5:")
    for m in state_migs:
        target = m[2].upper()
        if target in bin_by_cksum:
            print(f"  m{m[0]} {m[1]!r}: DB cksum FOUND in binary as anchor at @{bin_by_cksum[target][0]} L={bin_by_cksum[target][1]}")
        else:
            # Find a binary anchor whose first SQL line matches the migration description
            print(f"  m{m[0]} {m[1]!r}: DB cksum NOT in binary. (Will need replacement.)")

    print()
    print("LOGS_2:")
    for m in logs_migs:
        target = m[2].upper()
        if target in bin_by_cksum:
            print(f"  m{m[0]} {m[1]!r}: DB cksum FOUND in binary as anchor at @{bin_by_cksum[target][0]} L={bin_by_cksum[target][1]}")
        else:
            print(f"  m{m[0]} {m[1]!r}: DB cksum NOT in binary -> needs replacement")
            # Also try first line of SQL near description in binary
            descp = data.find(m[1].encode("utf-8"), region_start)
            if descp != -1 and descp < region_end:
                # SQL starts within ~100 bytes after the description string
                # Find the anchor nearest to descp
                candidates = [(sp, L, h) for sp, L, h in found if descp <= sp <= descp + 500]
                if candidates:
                    sp, L, h = min(candidates, key=lambda x: x[0])
                    print(f"      Likely binary anchor: @{sp} L={L} cksum={h}")

    data.close()
