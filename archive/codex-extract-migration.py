"""Extract migration 1 SQL and verify checksum match."""
import mmap
import hashlib

target = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

with open(target, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    # The single occurrence of "CREATE TABLE threads"
    p = data.find(b"CREATE TABLE threads")
    print(f"CREATE TABLE threads at offset {p}")
    print()

    # Print larger context around it - back 200 bytes for context, forward 4000 bytes for full migration
    print("=" * 70)
    print("Context BEFORE (-200):")
    print("=" * 70)
    before = data[max(p-200,0):p]
    print(repr(before.decode("utf-8", errors="replace")))
    print()

    # Get migration 1 SQL.
    # In sqlx-embedded Rust binaries, the SQL is stored as a length-prefixed
    # string. The length prefix is typically right before the start of SQL.
    # We search backwards for likely SQL boundary (null byte or length prefix).

    # Method: scan forward to find SQL boundary (first null byte after CREATE)
    # But CREATE TABLE multi-line SQL may contain ; - find proper ending
    print("=" * 70)
    print("Full SQL (forward 4000 bytes, until null):")
    print("=" * 70)
    forward = data[p:p+4000]
    # Find first null byte
    null = forward.find(b"\x00")
    if null == -1:
        sql_bytes = forward
    else:
        sql_bytes = forward[:null]
    sql_text = sql_bytes.decode("utf-8", errors="replace")
    print(sql_text)
    print()
    print(f"SQL length: {len(sql_bytes)} bytes")
    print()

    # Try several hash variants (sqlx might use SHA-384 with normalized/unnormalized text)
    print("=" * 70)
    print("Hash candidates:")
    print("=" * 70)
    print(f"DB stored migration 1 checksum (from new build):")
    print(f"  627EF19164C9BB298A0CD99945981C9B7BDA3D9E6CF12EB35145E3B1D3BF7CF8...")
    print()
    print(f"Other historical checksum (old 5.2MB DB, 2026-03):")
    print(f"  54BBD6F47905A4E4C674034575963D82DA7B534E66E9A37A81EC2AFB6A4B56CE...")
    print()
    print(f"SHA384(sql_text)            = {hashlib.sha384(sql_bytes).hexdigest().upper()[:96]}")
    print(f"SHA384(sql_text + nl)       = {hashlib.sha384(sql_bytes + b'\\n').hexdigest().upper()[:96]}")
    print(f"SHA384(sql.rstrip)          = {hashlib.sha384(sql_bytes.rstrip()).hexdigest().upper()[:96]}")
    print(f"SHA384(sql.strip)           = {hashlib.sha384(sql_bytes.strip()).hexdigest().upper()[:96]}")

    # Maybe SQL has trailing whitespace control characters
    sql_normalized = sql_bytes.decode("utf-8", errors="replace").strip().encode("utf-8")
    print(f"SHA384(sql.strip utf-8)     = {hashlib.sha384(sql_normalized).hexdigest().upper()[:96]}")

    print()
    print("=" * 70)
    print("Search for the EXACT byte sequence 627EF19164C9BB29 in binary")
    print("=" * 70)
    needle = bytes.fromhex("627EF19164C9BB298A0CD99945981C9B")
    found_at = []
    start = 0
    while True:
        pos = data.find(needle, start)
        if pos == -1:
            break
        found_at.append(pos)
        start = pos + 1
        if len(found_at) > 10:
            break
    print(f"  Found at: {found_at}")
    if found_at:
        for fa in found_at[:3]:
            ctx = data[max(fa-60,0):fa+100]
            print(f"  @{fa}: {ctx.hex()}")
            try:
                print(f"      text: {ctx.decode('utf-8', errors='replace')!r}")
            except: pass

    data.close()
