"""Manually backfill thread metadata from unindexed jsonl files.

Strategy: Read each jsonl's first line (session_meta) to get metadata, then
scan for the first user message to populate title/preview/first_user_message.
INSERT into threads. Finally mark backfill_state.status='complete' so GUI
launches immediately.

CRITICAL: Does NOT touch the jsonl files - only reads them. Does NOT touch
/home/god or any other user's data.
"""
import sqlite3
import os
import json
import time
import shutil
from datetime import datetime, timezone

DB = r"C:\Users\Xiao Difu\.codex\state_5.sqlite"
SESS = r"C:\Users\Xiao Difu\.codex\sessions"

# Backup state_5 first
ts = time.strftime("%Y%m%d-%H%M%S")
bk = DB + f".pre-manual-backfill-{ts}"
shutil.copy2(DB, bk)
print(f"Backed up state_5.sqlite to:\n  {bk}")
print()

# Read currently indexed rollout_paths
con = sqlite3.connect(DB, timeout=10)
cur = con.cursor()
cur.execute("SELECT rollout_path FROM threads")
indexed = set(row[0] for row in cur.fetchall())
print(f"Currently indexed: {len(indexed)} threads")

# Helper functions
def parse_iso(iso_str):
    """Parse ISO 8601 timestamp to (unix_secs, unix_ms)."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        secs = int(dt.timestamp())
        ms = int(dt.timestamp() * 1000)
        return secs, ms
    except Exception:
        return None, None

def win_to_lin(p):
    p = p.replace("\\", "/")
    if p.startswith("C:/") or p.startswith("c:/"):
        p = "/mnt/c" + p[2:]
    return p

def extract_first_user_message(jsonl_path, max_lines=200):
    """Scan up to max_lines of jsonl for first user content. Returns string or None."""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    obj = json.loads(line)
                except:
                    continue
                t = obj.get("type")
                p = obj.get("payload", {})
                if not isinstance(p, dict):
                    continue
                # Look for response_item with role=user
                if t == "response_item" and p.get("type") == "message":
                    if p.get("role") == "user":
                        content = p.get("content", [])
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                txt = c.get("text", "")
                                if txt and not txt.startswith("<"):
                                    return txt
                # Look for event_msg user input
                if t == "event_msg" and p.get("type") in ("user_message", "session_user_input"):
                    msg = p.get("message") or p.get("input") or p.get("text")
                    if msg:
                        return msg if isinstance(msg, str) else str(msg)
    except Exception:
        pass
    return None

def truncate(s, n):
    if s is None:
        return ""
    s = s.strip()
    return s[:n]

# Scan all jsonl files in sessions/
all_jsonls = []
for root, dirs, files in os.walk(SESS):
    for f in files:
        if f.endswith(".jsonl"):
            all_jsonls.append(os.path.join(root, f))
print(f"Total jsonl files in sessions/: {len(all_jsonls)}")

unindexed = []
for fp in all_jsonls:
    lp = win_to_lin(fp)
    if lp not in indexed:
        unindexed.append((fp, lp))
print(f"UNINDEXED jsonl files: {len(unindexed)}")
print()

# Process each unindexed file
inserted = 0
skipped = []
for win_path, lin_path in unindexed:
    try:
        with open(win_path, "r", encoding="utf-8") as f:
            first_line = f.readline()
        meta = json.loads(first_line)
        if meta.get("type") != "session_meta":
            skipped.append((win_path, "first line not session_meta"))
            continue
        payload = meta.get("payload", {})
        thread_id = payload.get("id")
        if not thread_id:
            skipped.append((win_path, "no id"))
            continue
        # created_at: prefer payload.timestamp, fall back to meta.timestamp
        ts_str = payload.get("timestamp") or meta.get("timestamp")
        created_secs, created_ms = parse_iso(ts_str)
        if created_secs is None:
            # Use file mtime
            created_secs = int(os.path.getmtime(win_path))
            created_ms = created_secs * 1000
        # updated_at: file mtime (approximate)
        updated_secs = int(os.path.getmtime(win_path))
        updated_ms = updated_secs * 1000

        cwd = payload.get("cwd", "/")
        source = payload.get("source", "unknown")
        model_provider = payload.get("model_provider", "openai")
        cli_version = payload.get("cli_version", "")
        thread_source = payload.get("thread_source", "user")

        first_user_msg = extract_first_user_message(win_path) or ""
        title = truncate(first_user_msg, 200) or os.path.basename(win_path)
        preview = truncate(first_user_msg, 200) or ""
        first_user_message_db = truncate(first_user_msg, 1000)

        has_user_event = 1 if first_user_msg else 0

        # Insert
        cur.execute("""
            INSERT OR IGNORE INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider,
                cwd, title, sandbox_policy, approval_mode, tokens_used, has_user_event,
                archived, archived_at, git_sha, git_branch, git_origin_url,
                cli_version, first_user_message, memory_mode, model, reasoning_effort,
                created_at_ms, updated_at_ms, thread_source, preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, NULL, NULL, NULL, NULL, ?, ?, 'enabled', NULL, NULL, ?, ?, ?, ?)
        """, (
            thread_id, lin_path, created_secs, updated_secs, source, model_provider,
            cwd, title, '{"type":"danger-full-access"}', "never", has_user_event,
            cli_version, first_user_message_db,
            created_ms, updated_ms, thread_source, preview
        ))
        if cur.rowcount > 0:
            inserted += 1
            print(f"  [{inserted:3d}/{len(unindexed)}] {os.path.basename(win_path)[:70]} -> title={truncate(title, 50)!r}")
    except Exception as e:
        skipped.append((win_path, f"err: {e}"))
        print(f"  SKIP {os.path.basename(win_path)}: {e}")

con.commit()

print()
print(f"Inserted: {inserted} new threads")
print(f"Skipped:  {len(skipped)} files")
if skipped:
    for fp, reason in skipped[:5]:
        print(f"  {os.path.basename(fp)}: {reason}")

# Verify final count
cur.execute("SELECT COUNT(*) FROM threads")
total = cur.fetchone()[0]
print(f"\nTotal threads now: {total}")

# Mark backfill complete
print()
print("=" * 70)
print("Marking backfill_state.status = 'complete'")
print("=" * 70)
cur.execute("SELECT status, last_watermark, last_success_at FROM backfill_state")
before = cur.fetchone()
print(f"BEFORE: {before}")

now = int(time.time())
# Pick the newest jsonl path as watermark
newest_jsonl = max(all_jsonls, key=os.path.getmtime)
newest_rel = "sessions" + newest_jsonl.replace("\\", "/").split("/sessions", 1)[1]
cur.execute("""
    UPDATE backfill_state
    SET status = 'complete', last_watermark = ?, last_success_at = ?, updated_at = ?
""", (newest_rel, now, now))
con.commit()

cur.execute("SELECT status, last_watermark, last_success_at FROM backfill_state")
after = cur.fetchone()
print(f"AFTER:  {after}")

con.close()
print()
print("Done.")
