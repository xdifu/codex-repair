"""Find backfill timeout config and retry logic in binary."""
import mmap

new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    # All backfill-related strings
    for needle in [
        b"startup_initialization",
        b"state_db_backfill",
        b"backfill_status",
        b"backfill_wait",
        b"backfill_max",
        b"BackfillStatus",
        b"BackfillState",
        b"BackfillConfig",
        b"BackfillSettings",
        b"backfill.toml",
        b"state_runtime",
        b"StateRuntime",
        b"state runtime",
        b"initialization_timeout",
        b"InitializationTimeout",
        b"INITIALIZATION_TIMEOUT",
        b"CODEX_INITIALIZATION",
        b"CODEX_BACKFILL",
        b"backfill_running",
        b"BackfillRunning",
        b"BackfillTimeout",
        b"Pending",
        b"Running",
        b"Completed",
        b"Failed",
        b"Idle",
        b"backfill.rs:",
        b"runtime.rs:",
    ]:
        start = 0
        cnt = 0
        while cnt < 3:
            pos = data.find(needle, start)
            if pos == -1:
                break
            cnt += 1
            ctx = data[max(pos-80,0):pos+250]
            print(f"\n  {needle!r} @{pos}:")
            try:
                print(f"    {ctx.decode('utf-8', errors='replace')!r}")
            except:
                print(f"    {ctx!r}")
            start = pos + 1

    # Look for time-related literals: 30, 60, 300, etc., right next to "backfill"
    print()
    print("=" * 70)
    print("Search 'state db backfill is ' context:")
    print("=" * 70)
    p = data.find(b"state db backfill is ")
    if p != -1:
        ctx = data[max(p-200,0):p+500]
        print(f"  @{p}: {ctx.decode('utf-8', errors='replace')!r}")

    print()
    print("=" * 70)
    print("Search 'retrying startup' context (look for retry counts and durations):")
    print("=" * 70)
    p = data.find(b"retrying startup")
    while p != -1:
        ctx = data[max(p-400,0):p+200]
        print(f"  @{p}: {ctx.decode('utf-8', errors='replace')!r}")
        p = data.find(b"retrying startup", p+1)

    # See if there is a config TOML key for backfill
    print()
    print("=" * 70)
    print("All TOML-style keys mentioning 'backfill' or 'state':")
    print("=" * 70)
    import re
    # Search for patterns like 'backfill_*' assigned to integer in source
    starts = []
    for pattern in [b"backfill_", b"state_db_", b"runtime_"]:
        s = 0
        cnt = 0
        while cnt < 30:
            p = data.find(pattern, s)
            if p == -1:
                break
            # only show printable ASCII context
            ctx = data[p:p+80]
            try:
                t = ctx.decode("utf-8")
                if all(c.isprintable() or c in "\n\r\t " for c in t[:60]):
                    print(f"  @{p}: {t[:60]!r}")
                    cnt += 1
            except:
                pass
            s = p + 1
    data.close()
