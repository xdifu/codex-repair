"""Analyze current backfill state and determine remaining work."""
import sqlite3
import os
import mmap

db = r"C:\Users\Xiao Difu\.codex\state_5.sqlite"
sessions_root = r"C:\Users\Xiao Difu\.codex\sessions"
new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
cur = con.cursor()

cur.execute("SELECT COUNT(*) FROM threads")
n_threads = cur.fetchone()[0]
print(f"threads count: {n_threads}")

cur.execute("SELECT MIN(created_at), MAX(created_at) FROM threads WHERE created_at > 0")
print(f"threads created_at range: {cur.fetchone()}")

cur.execute("SELECT id, rollout_path, created_at, title FROM threads ORDER BY created_at DESC LIMIT 5")
print()
print("Newest 5 threads in DB:")
for r in cur.fetchall():
    print(f"  {r[0][:30]}  rollout={r[1][:80]}")
    print(f"     created_at={r[2]}, title={(r[3] or '')[:60]!r}")

cur.execute("SELECT id, rollout_path FROM threads ORDER BY created_at ASC LIMIT 3")
print()
print("Oldest 3 threads in DB:")
for r in cur.fetchall():
    print(f"  {r[0][:30]}  rollout={r[1][:80]}")

cur.execute("SELECT status, last_watermark, last_success_at, updated_at FROM backfill_state")
bf = cur.fetchone()
print()
print(f"backfill_state:")
print(f"  status={bf[0]!r}")
print(f"  last_watermark={bf[1]!r}")
print(f"  last_success_at={bf[2]}")
print(f"  updated_at={bf[3]}")

con.close()
print()

# Now list all jsonl files and figure out which are NOT yet indexed
print("=" * 70)
print("Total jsonl files in sessions:")
print("=" * 70)
all_jsonl = []
for root, dirs, files in os.walk(sessions_root):
    for f in files:
        if f.endswith(".jsonl"):
            fp = os.path.join(root, f).replace("\\", "/")
            # Make path relative to .codex
            rel = fp[fp.find("/sessions/"):]
            all_jsonl.append((fp, rel, os.path.getsize(fp), os.path.getmtime(fp)))
print(f"  Total: {len(all_jsonl)} jsonl files")

# Show files SORTED by modification time descending
all_jsonl.sort(key=lambda x: x[3], reverse=True)
print()
print("Newest 10 jsonl files:")
for fp, rel, sz, mt in all_jsonl[:10]:
    import time
    print(f"  {sz:>12d} bytes  {time.ctime(mt)}  {rel}")

print()
# Search binary for backfill timeout / config
print("=" * 70)
print("Search binary for backfill timeout / config strings:")
print("=" * 70)
with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    for needle in [
        b"timed out waiting for state db backfill",
        b"backfill_timeout",
        b"backfill timeout",
        b"after %ds",
        b"after 30s",
        b"BACKFILL_TIMEOUT",
        b"CODEX_BACKFILL",
        b"after \xff",
        b"30s",
    ]:
        start = 0
        cnt = 0
        while cnt < 3:
            pos = data.find(needle, start)
            if pos == -1:
                break
            cnt += 1
            ctx = data[max(pos-100,0):pos+200].decode("utf-8", errors="replace")
            print(f"\n  {needle!r}  @{pos}:")
            print(f"    {ctx!r}")
            start = pos + 1

    # Also: look for known config keys
    for needle in [b"backfill", b"BackfillConfig", b"BackfillState", b"BackfillStatus"]:
        s = 0
        cnt = 0
        while cnt < 5:
            pos = data.find(needle, s)
            if pos == -1:
                break
            cnt += 1
            ctx = data[max(pos-50,0):pos+150]
            print(f"\n  {needle!r} @{pos}: {ctx[:180]!r}")
            s = pos + 1
    data.close()
