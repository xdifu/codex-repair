"""Find which jsonl files are NOT indexed in the threads table, and read their metadata."""
import sqlite3
import os
import json
import time

DB = r"C:\Users\Xiao Difu\.codex\state_5.sqlite"
SESS = r"C:\Users\Xiao Difu\.codex\sessions"
ARCH = r"C:\Users\Xiao Difu\.codex\archived_sessions"

# Read all indexed rollout_paths
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
cur = con.cursor()
cur.execute("SELECT id, rollout_path, created_at, title FROM threads")
indexed = {row[1]: row for row in cur.fetchall()}
print(f"Indexed thread count: {len(indexed)}")

# Show schema
cur.execute("PRAGMA table_info(threads)")
print("\nthreads columns:")
cols = cur.fetchall()
for c in cols:
    print(f"  {c}")
con.close()

# Compute the set of all jsonl files
all_files = []
for root, dirs, files in os.walk(SESS):
    for f in files:
        if f.endswith(".jsonl"):
            all_files.append(os.path.join(root, f))
print(f"\nTotal jsonl in sessions/: {len(all_files)}")

# Map to Linux path format (used in DB)
def win_to_lin(p):
    p = p.replace("\\", "/")
    if p.startswith("C:/"):
        p = "/mnt/c" + p[2:]
    return p

unindexed_files = []
for fp in all_files:
    lp = win_to_lin(fp)
    if lp not in indexed:
        unindexed_files.append(fp)

print(f"\nUNINDEXED jsonl files: {len(unindexed_files)}")
for fp in unindexed_files:
    sz = os.path.getsize(fp)
    mt = os.path.getmtime(fp)
    print(f"\n  {fp}")
    print(f"    size={sz/1024/1024:.1f} MB, mtime={time.ctime(mt)}")
    # Read first JSON line and last JSON line to extract metadata
    try:
        with open(fp, "rb") as f:
            first_chunk = f.read(8192)
        first_line = first_chunk.split(b"\n", 1)[0]
        try:
            first_obj = json.loads(first_line.decode("utf-8"))
        except:
            first_obj = None

        if first_obj:
            keys = list(first_obj.keys())[:15]
            print(f"    first line keys: {keys}")
            if "session_id" in first_obj:
                print(f"    session_id: {first_obj['session_id']}")
            if "id" in first_obj:
                print(f"    id: {first_obj['id']}")
            if "type" in first_obj:
                print(f"    type: {first_obj['type']}")
            if "cwd" in first_obj:
                print(f"    cwd: {first_obj['cwd']!r}")
            if "timestamp" in first_obj:
                print(f"    timestamp: {first_obj['timestamp']}")
            if "model" in first_obj:
                print(f"    model: {first_obj['model']}")
            if "instructions" in first_obj:
                v = first_obj["instructions"]
                if isinstance(v, str):
                    print(f"    instructions: {v[:80]!r}")
    except Exception as e:
        print(f"    ERR reading: {e}")
