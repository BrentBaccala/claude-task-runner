#!/usr/bin/env python3
"""Print a bar chart of turns used by task runs."""

import argparse
from datetime import datetime
import os
import signal
import shutil
import sqlite3
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from project_dir import DB_PATH

STATUS_MARKERS = {
    "success": " ",
    "failure": "x",
    "timeout": "T",
    "max_turns": "M",
    "usage_limit": "U",
    "interrupted": "I",
    None: "?",
}


def get_runs(db, task_name=None, sort_by="id"):
    query = """
        SELECT r.id, t.name, r.num_turns, r.result_status, r.cost_usd,
               r.started_at, r.duration_ms
        FROM runs r JOIN tasks t ON r.task_id = t.id
        WHERE r.num_turns IS NOT NULL
    """
    params = []
    if task_name:
        query += " AND t.name = ?"
        params.append(task_name)

    if sort_by == "turns":
        query += " ORDER BY r.num_turns DESC, r.id"
    elif sort_by == "task":
        query += " ORDER BY t.name, r.id"
    else:
        query += " ORDER BY r.id"

    return db.execute(query, params).fetchall()


def print_chart(runs, term_width, show_time=False):
    if not runs:
        print("No runs with turn data.")
        return

    max_turns = max(r["num_turns"] for r in runs)

    label_width = min(28, max(len(r["name"]) for r in runs))
    # base: id(3) + sp + dur(4) + sp + name + sp + marker(1) + sp + turns(3) + " |"
    prefix_len = 4 + 5 + label_width + 4 + 3 + 2
    if show_time:
        prefix_len += 11  # "DDMon HH:MM "
    bar_width = max(10, term_width - prefix_len - 1)

    for r in runs:
        name = r["name"][:label_width].ljust(label_width)
        marker = STATUS_MARKERS.get(r["result_status"], "?")
        turns = r["num_turns"]
        bar_len = round(turns / max_turns * bar_width) if max_turns > 0 else 0

        # Format start time
        if show_time and r["started_at"]:
            try:
                dt = datetime.fromisoformat(r["started_at"])
                start = dt.strftime("%d%b %H:%M") + " "
            except (ValueError, TypeError):
                start = "           "
        elif show_time:
            start = "           "
        else:
            start = ""

        # Format duration
        if r["duration_ms"] and r["duration_ms"] > 0:
            dur_min = r["duration_ms"] / 60000
            dur = f"{dur_min:3.0f}m"
        else:
            dur = "    "

        if r["result_status"] == "success":
            bar_char = "█"
        elif r["result_status"] == "failure":
            bar_char = "░"
        else:
            bar_char = "▒"

        print(f"{r['id']:3d} {start}{dur} {name} {marker} {turns:3d} |{bar_char * bar_len}")

    print()
    print(f"  {len(runs)} runs, max {max_turns} turns")
    print(f"  █ success  ░ failure  ▒ other  x=fail T=timeout M=max_turns U=usage_limit ?=unknown")


def print_histogram(runs, term_width, bucket_size=10):
    if not runs:
        print("No runs with turn data.")
        return

    max_turns = max(r["num_turns"] for r in runs)
    num_buckets = max_turns // bucket_size + 1

    buckets = [0] * num_buckets
    for r in runs:
        b = r["num_turns"] // bucket_size
        buckets[b] += 1

    max_count = max(buckets)
    label_width = len(f"{(num_buckets - 1) * bucket_size}-{num_buckets * bucket_size - 1}")
    prefix_len = label_width + 2 + 3 + 2  # " | "
    bar_width = max(10, term_width - prefix_len - 1)

    for i, count in enumerate(buckets):
        if count == 0 and i > 0 and all(c == 0 for c in buckets[i:]):
            break
        lo = i * bucket_size
        hi = (i + 1) * bucket_size - 1
        label = f"{lo}-{hi}".rjust(label_width)
        bar_len = round(count / max_count * bar_width) if max_count > 0 else 0
        print(f"{label} {count:3d} |{'█' * bar_len}")

    print()
    print(f"  {len(runs)} runs, bucket size = {bucket_size} turns")


def main():
    parser = argparse.ArgumentParser(description="Bar chart of turns used by task runs.")
    parser.add_argument("--task", help="Filter to a specific task name")
    parser.add_argument("--sort", choices=["id", "turns", "task"], default="id",
                        help="Sort order (default: run id)")
    parser.add_argument("-t", "--time", action="store_true",
                        help="Show start time column")
    parser.add_argument("--histogram", action="store_true",
                        help="Show histogram (distribution) instead of per-run bars")
    parser.add_argument("--bucket", type=int, default=10,
                        help="Histogram bucket size (default: 10)")
    parser.add_argument("--width", type=int, default=0,
                        help="Terminal width override")
    args = parser.parse_args()

    term_width = args.width or shutil.get_terminal_size().columns

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    runs = get_runs(db, task_name=args.task, sort_by=args.sort)

    if args.histogram:
        print_histogram(runs, term_width, args.bucket)
    else:
        print_chart(runs, term_width, show_time=args.time)


if __name__ == "__main__":
    main()
