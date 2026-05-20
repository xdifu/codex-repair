"""Find codex CLI subcommands."""
import mmap

new_codex = r"C:\Users\Xiao Difu\.codex\bin\wsl\7945a00f33bdc140\codex"

with open(new_codex, "rb") as f:
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    # Common CLI patterns
    patterns = [
        b"app-server",
        b"app_server",
        b"--help",
        b"--version",
        b"Codex CLI",
        b"Usage:",
        b"USAGE:",
        b"backfill",
        b"reindex",
        b"sessions",
        b"index_sessions",
        b"--backfill",
        b"--reindex",
        b"--repair",
        b"--rebuild",
        b"login",
        b"logout",
        b"about",
        b"version",
        b"exec",
        b"completion",
        b"daemon",
    ]
    print("Search for CLI patterns:")
    for needle in patterns:
        positions = []
        s = 0
        while True:
            p = data.find(needle, s)
            if p == -1:
                break
            positions.append(p)
            s = p + 1
            if len(positions) > 5:
                break
        if positions:
            for p in positions[:3]:
                ctx = data[max(p-50,0):p+200]
                try:
                    t = ctx.decode("utf-8", errors="replace")
                    if "\x00" * 4 not in t:
                        print(f"\n  {needle!r} @{p}: {t!r}")
                except: pass

    # Try to find clap (Rust CLI) subcommand definitions
    print()
    print("=" * 70)
    print("Clap/CLI subcommand strings:")
    print("=" * 70)
    # Clap stores subcommand names as inline strings, often near "Subcommand" type
    # Look for arg names like "command" near typical CLI patterns
    for needle in [b"SUBCOMMANDS:", b"subcommand", b"AboutSubcommand", b"about\x00", b"login\x00", b"logout\x00"]:
        p = data.find(needle)
        if p != -1:
            ctx = data[max(p-100,0):p+400]
            print(f"\n  {needle!r} @{p}: {ctx[:500].decode('utf-8', errors='replace')!r}")

    data.close()
