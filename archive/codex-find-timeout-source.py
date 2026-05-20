"""Find the source of the 30s timeout in backfill."""
import mmap
import re

new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    # Search for things like Duration::from_secs(30) - in Rust source, these are constants
    # In compiled binary, integer 30 is probably embedded as immediate value next to backfill code
    # Look for "backfill" path and surrounding strings:
    print("All backfill-related strings:")
    p = data.find(b"backfill")
    cnt = 0
    while p != -1 and cnt < 50:
        # Walk back to find start of contiguous printable string
        s = p
        while s > 0 and (data[s-1] >= 0x20 and data[s-1] < 0x7f or data[s-1] in (0x09, 0x0A)):
            s -= 1
        # Walk forward
        e = p
        while e < len(data) and (data[e] >= 0x20 and data[e] < 0x7f or data[e] in (0x09, 0x0A)):
            e += 1
        if e - s > 10 and e - s < 400:
            txt = data[s:e].decode("utf-8", errors="replace")
            if "backfill" in txt.lower():
                print(f"  @{s}-{e}: {txt!r}")
                cnt += 1
        p = data.find(b"backfill", e + 1)

    # Look for "max_wait" or similar
    print()
    print("=" * 70)
    print("Look for env var / config strings near backfill timeout:")
    print("=" * 70)
    for needle in [
        b"CODEX_STATE_DB_BACKFILL",
        b"CODEX_BACKFILL_TIMEOUT",
        b"CODEX_BACKFILL_WAIT",
        b"CODEX_STARTUP",
        b"backfill_timeout_secs",
        b"backfill_wait_secs",
        b"startup_wait_secs",
        b"backfill_init_timeout",
        b"max_backfill_wait",
        b"BACKFILL_INIT",
        b"BackfillInit",
        b"start_backfill",
        b"wait_for_backfill",
        b"WAIT_FOR_BACKFILL",
    ]:
        p = data.find(needle)
        if p != -1:
            ctx = data[max(p-200,0):p+400]
            print(f"\n  {needle!r} @{p}: {ctx.decode('utf-8', errors='replace')!r}")

    # Also list all integer literals near "state db backfill" string
    error_str_pos = data.find(b"timed out waiting for state db backfill")
    if error_str_pos != -1:
        # search 50000 bytes backward for a likely Duration immediate (e.g. 30 = 0x1E)
        print()
        print(f"timed out waiting... at {error_str_pos}")

    data.close()
