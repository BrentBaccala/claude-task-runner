#!/usr/bin/env python3
"""Pretty-print ~/.claude/history.jsonl (user messages only)."""

import json
import argparse
import os
from datetime import datetime

def main():
    parser = argparse.ArgumentParser(description="Pretty-print Claude history.jsonl")
    parser.add_argument("--session", "-s", help="Filter to a specific session ID (prefix match)")
    parser.add_argument("--file", "-f", default=os.path.expanduser("~/.claude/history.jsonl"),
                        help="Path to history.jsonl")
    parser.add_argument("--list-sessions", "-l", action="store_true",
                        help="List sessions with message counts and date ranges")
    args = parser.parse_args()

    with open(args.file) as f:
        entries = [json.loads(line) for line in f]

    if args.list_sessions:
        sessions = {}
        for e in entries:
            sid = e.get("sessionId", "unknown")
            ts = e["timestamp"]
            if sid not in sessions:
                sessions[sid] = {"count": 0, "first": ts, "last": ts}
            sessions[sid]["count"] += 1
            sessions[sid]["first"] = min(sessions[sid]["first"], ts)
            sessions[sid]["last"] = max(sessions[sid]["last"], ts)
        for sid, info in sorted(sessions.items(), key=lambda x: x[1]["first"]):
            first = datetime.fromtimestamp(info["first"] / 1000).strftime("%Y-%m-%d %H:%M")
            last = datetime.fromtimestamp(info["last"] / 1000).strftime("%H:%M")
            print(f"{sid}  {info['count']:3d} msgs  {first} – {last}")
        return

    if args.session:
        entries = [e for e in entries if e.get("sessionId", "").startswith(args.session)]

    last_session = None
    for e in entries:
        sid = e.get("sessionId", "unknown")
        ts = datetime.fromtimestamp(e["timestamp"] / 1000)
        display = e.get("display", "").strip()

        if sid != last_session:
            print(f"\n{'=' * 60}")
            print(f"Session: {sid}")
            print(f"{'=' * 60}")
            last_session = sid

        print(f"\n[{ts}]")
        print(display)

if __name__ == "__main__":
    main()
