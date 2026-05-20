"""For each DB-stored migration checksum, do a byte-level search in the codex binary."""
import mmap
import sqlite3

new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"
dbs = {
    "state_5": r"C:\Users\Xiao Difu\.codex\state_5.sqlite",
    "logs_2":  r"C:\Users\Xiao Difu\.codex\logs_2.sqlite",
}

# Read all DB-stored checksums
all_db = {}
for name, path in dbs.items():
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("SELECT version, description, hex(checksum) FROM _sqlx_migrations ORDER BY version")
    all_db[name] = cur.fetchall()
    con.close()

print(f"Total state_5 migs: {len(all_db['state_5'])}")
print(f"Total logs_2 migs:  {len(all_db['logs_2'])}")
print()

with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    for db_name, migs in all_db.items():
        print("=" * 70)
        print(f"DB: {db_name}")
        print("=" * 70)
        for ver, desc, cksum_hex in migs:
            cksum_bytes = bytes.fromhex(cksum_hex)
            # Search entire binary
            pos = data.find(cksum_bytes)
            if pos == -1:
                print(f"  m{ver:2d} {desc!r:45s}: NOT FOUND in binary (cksum={cksum_hex[:32]}...)")
            else:
                # Show ~80 bytes before the checksum to see preceding SQL
                ctx_before = data[max(pos-200, 0):pos].decode("utf-8", errors="replace")
                # And the description that follows the checksum (next migration's description)
                ctx_after = data[pos+48:pos+48+100].decode("utf-8", errors="replace")
                print(f"  m{ver:2d} {desc!r:45s}: FOUND @ {pos} cksum={cksum_hex[:32]}...")
                # if anchor failed but byte still found, show context
                last_line = ctx_before.split("\n")[-1] if "\n" in ctx_before else ctx_before[-100:]
                next_desc = ctx_after.split("\x00")[0][:60] if "\x00" in ctx_after else ctx_after[:60]
                # print(f"      preceded by ...{last_line!r}")
                # print(f"      followed by {next_desc!r}")
        print()
    data.close()
