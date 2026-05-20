"""Compare migration 1 SQL+checksum between OLD wsl/codex and NEW wsl/7945a00f33bdc140/codex."""
import mmap
import hashlib

files = {
    "OLD wsl/codex (2026-05-09)":         r"C:\Users\Xiao Difu\.codex\bin\wsl\codex",
    "NEW wsl/7945a00f33bdc140/codex":     r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex",
    "MSIX app/resources/codex":           r"C:\Program Files\WindowsApps\OpenAI.Codex_26.519.2081.0_x64__2p2nqsd0c76g0\app\resources\codex",
}

for label, path in files.items():
    print()
    print("=" * 70)
    print(f" {label}")
    print(f" {path}")
    print("=" * 70)
    try:
        with open(path, "rb") as f:
            data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            p = data.find(b"CREATE TABLE threads")
            if p == -1:
                print("  CREATE TABLE threads NOT found!")
                data.close()
                continue
            # Find a SHA384-sized run of pseudo-random bytes after the SQL.
            # We know SQL ends with ");\n" or similar and is followed by 48 bytes hash.
            # Simpler: find next occurrence of "CREATE TABLE logs" - this is migration 2 SQL start
            q = data.find(b"CREATE TABLE logs", p)
            if q == -1:
                q = data.find(b"\x00", p + 800)
            sql_bytes = data[p:q]
            # strip trailing 48 + description bytes that belong to next migration metadata
            # Use exact match: in sqlx, checksum is right after SQL, then next migration starts
            # Heuristic: take 863 bytes (we already know NEW one is 863)
            # But OLD might differ - so try sha384 of trailing slices to find a checksum
            print(f"  CREATE TABLE threads at offset {p}")
            print(f"  Next CREATE TABLE logs/null at {q} (distance {q-p})")
            # Try shrinking: for each candidate sql length L = 800..900, compute SHA384 of data[p:p+L]
            # and check if the 48 bytes right after equal that hash.
            found = False
            for L in range(600, 1500):
                sql_try = data[p:p+L]
                checksum_pos = p + L
                actual = data[checksum_pos:checksum_pos+48]
                computed = hashlib.sha384(sql_try).digest()
                if actual == computed:
                    print(f"  >>> MATCH: SQL length={L}, hardcoded checksum = {actual.hex().upper()}")
                    found = True
                    break
            if not found:
                print("  Could not auto-locate SQL/checksum boundary.")
            data.close()
    except Exception as e:
        print(f"  ERR: {e}")
