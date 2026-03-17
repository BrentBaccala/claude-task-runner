#!/usr/bin/env python3
"""Export interactive and plan-implementation session logs into ~/project.

Creates hardlinks from .claude session files into:
  ~/project/sessions/       — interactive sessions
  ~/project/plan-sessions/  — plan implementation sessions

Hardlinks are used because .jsonl files are append-only, so the link
always reflects the current state without copying.

Also auto-detects display names:
  - From /rename commands in ~/.claude/history.jsonl
  - From plan titles (# Plan: ...) in plan implementation sessions
"""

import json
import os
import re
import sqlite3
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from project_dir import PROJECT_DIR, DB_PATH
CLAUDE_DIR = os.path.expanduser("~/.claude")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "projects/-home-claude")
MEMORY_DIR = os.path.join(SESSIONS_DIR, "memory")
HISTORY_FILE = os.path.join(CLAUDE_DIR, "history.jsonl")

INTERACTIVE_DIR = os.path.join(PROJECT_DIR, "sessions")
PLAN_DIR = os.path.join(PROJECT_DIR, "plan-sessions")
MEMORY_EXPORT_DIR = os.path.join(PROJECT_DIR, "memory")


def get_renames():
    """Extract /rename commands from history.jsonl."""
    renames = {}
    if not os.path.exists(HISTORY_FILE):
        return renames
    with open(HISTORY_FILE) as fh:
        for line in fh:
            try:
                evt = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(evt, dict):
                continue
            display = evt.get("display", "")
            sid = evt.get("sessionId", "")
            if display.startswith("/rename ") and sid:
                renames[sid] = display[len("/rename "):].strip()
    return renames


def get_plan_title(path):
    """Extract plan title from first user message if it's a plan implementation."""
    with open(path) as fh:
        for line in fh:
            try:
                evt = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(evt, dict):
                continue
            if evt.get("type") != "user":
                continue
            msg = evt.get("message", "")
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, list):
                    texts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text"]
                    content = " ".join(texts)
                msg = content
            if not isinstance(msg, str):
                continue
            if "Implement the following plan" not in msg:
                return None
            # Find the first markdown header
            for mline in msg.split("\n"):
                m = re.match(r"^#\s+(.+)", mline)
                if m:
                    title = m.group(1).strip()
                    title = re.sub(r"^Plan:\s*", "", title)
                    # Clean up backticks and special chars for filenames
                    title = title.strip("`")
                    return title
            return "unknown-plan"
    return None


def safe_filename(name):
    """Convert a display name to a safe filename."""
    # Replace characters that are problematic in filenames
    name = name.replace("/", "-").replace("\\", "-")
    name = name.replace(":", "-").replace("*", "").replace("?", "")
    name = name.replace('"', "").replace("<", "").replace(">", "")
    name = name.replace("|", "-")
    # Collapse multiple hyphens/spaces
    name = re.sub(r"[-\s]+", "-", name)
    name = name.strip("-")
    return name


def export_memory(dry_run=False):
    """Hardlink memory files from .claude into ~/project/memory/."""
    if not os.path.isdir(MEMORY_DIR):
        return 0
    os.makedirs(MEMORY_EXPORT_DIR, exist_ok=True)
    created = 0
    for fname in os.listdir(MEMORY_DIR):
        src = os.path.join(MEMORY_DIR, fname)
        if not os.path.isfile(src):
            continue
        dest = os.path.join(MEMORY_EXPORT_DIR, fname)
        if os.path.exists(dest):
            if os.path.samefile(src, dest):
                continue
            # Different file — remove stale link
            if not dry_run:
                os.unlink(dest)
        action = "WOULD link" if dry_run else "link"
        print(f"  {action} memory/{fname}")
        if not dry_run:
            os.link(src, dest)
        created += 1
    # Remove links whose source no longer exists
    for fname in os.listdir(MEMORY_EXPORT_DIR):
        src = os.path.join(MEMORY_DIR, fname)
        dest = os.path.join(MEMORY_EXPORT_DIR, fname)
        if not os.path.exists(src):
            action = "WOULD remove" if dry_run else "remove"
            print(f"  {action} stale memory/{fname}")
            if not dry_run:
                os.unlink(dest)
    return created


def main():
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Get all non-task sessions with messages
    sessions = db.execute("""
        SELECT session_id, display_name, custom_title, is_task, first_user_msg
        FROM sessions
        WHERE has_messages = 1 AND is_task = 0 AND deleted = 0
        ORDER BY first_ts
    """).fetchall()

    renames = get_renames()

    os.makedirs(INTERACTIVE_DIR, exist_ok=True)
    os.makedirs(PLAN_DIR, exist_ok=True)

    created = 0
    skipped = 0
    updated_names = 0

    for sess in sessions:
        sid = sess["session_id"]
        src = os.path.join(SESSIONS_DIR, sid + ".jsonl")
        if not os.path.exists(src):
            continue

        # Determine session type and name
        display_name = sess["display_name"]
        custom_title = sess["custom_title"]

        # Check if it's a plan implementation
        plan_title = get_plan_title(src)
        is_plan = plan_title is not None

        # An existing "plan: " display_name also marks it as a plan
        if display_name and display_name.startswith("plan: "):
            is_plan = True

        # A /rename overrides plan detection — user explicitly chose a name,
        # so treat it as interactive (e.g., gnumach started as a plan but was renamed)
        if sid in renames:
            is_plan = False

        # Determine display name (priority: existing display_name > /rename > plan title)
        if not display_name:
            if sid in renames:
                display_name = renames[sid]
            elif is_plan:
                display_name = "plan: " + plan_title
            elif custom_title:
                display_name = custom_title

        if not display_name:
            skipped += 1
            continue

        # Update display_name in DB if not already set
        if not sess["display_name"] and display_name:
            if not dry_run:
                db.execute("UPDATE sessions SET display_name = ? WHERE session_id = ?",
                           (display_name, sid))
            updated_names += 1

        # Determine target directory and filename
        if is_plan:
            target_dir = PLAN_DIR
            # Strip "plan: " prefix for the filename since directory says it's a plan
            fname = display_name
            if fname.startswith("plan: "):
                fname = fname[len("plan: "):]
        else:
            target_dir = INTERACTIVE_DIR
            fname = display_name

        fname = safe_filename(fname) + ".jsonl"
        dest = os.path.join(target_dir, fname)

        # Check if link already exists and points to the same file
        if os.path.exists(dest):
            if os.path.samefile(src, dest):
                skipped += 1
                continue
            else:
                # Different file with the same name — add session ID suffix
                base = safe_filename(fname.replace(".jsonl", ""))
                fname = f"{base}-{sid[:8]}.jsonl"
                dest = os.path.join(target_dir, fname)
                if os.path.exists(dest) and os.path.samefile(src, dest):
                    skipped += 1
                    continue

        action = "WOULD link" if dry_run else "link"
        print(f"  {action} {display_name} -> {os.path.relpath(dest, PROJECT_DIR)}")

        if not dry_run:
            os.link(src, dest)
        created += 1

    if not dry_run:
        db.commit()
    db.close()

    memory_created = export_memory(dry_run)

    print()
    print(f"  {created} sessions linked, {skipped} skipped, {updated_names} names updated")
    if memory_created:
        print(f"  {memory_created} memory files linked")
    if dry_run:
        print("  (dry run — no changes made)")


if __name__ == "__main__":
    main()
