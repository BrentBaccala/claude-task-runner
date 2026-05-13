#!/usr/bin/env python3
"""Format a Claude Code session jsonl file for readable terminal output.

Renders session transcripts with the same visual style as the interactive
Claude Code CLI:

    ❯ user input here

    ● Assistant text here, word-wrapped to terminal width

    ● Read(file_path: "/etc/issue")

      ⎿  Debian GNU/Linux trixie/sid \\n \\l

    ● More assistant text
"""

import json
import os
import pathlib
import re
import sqlite3
import sys
import textwrap
import argparse
from datetime import datetime


def get_terminal_width():
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def truncate(s, maxlen):
    """Truncate string to maxlen, adding ... if truncated."""
    if len(s) <= maxlen:
        return s
    return s[: maxlen - 3] + "..."


def format_tool_args(name, input_dict, truncate_strings=True):
    """Format tool_use input as a readable arg summary.

    When `truncate_strings` is False, strings are not cut to 60 chars —
    long Bash commands and Grep patterns render in full (wrapped by caller).
    """
    if not isinstance(input_dict, dict):
        return ""

    # Pick the most informative args for each tool type
    key_args = {
        "Read": ["file_path", "offset", "limit"],
        "Write": ["file_path"],
        "Edit": ["file_path"],
        "Glob": ["pattern", "path"],
        "Grep": ["pattern", "path", "output_mode"],
        "Bash": ["command"],
        "WebFetch": ["url"],
        "WebSearch": ["query"],
        "Task": ["description", "subagent_type"],
        "NotebookEdit": ["notebook_path"],
    }

    # Use known key args if available, otherwise show all
    if name in key_args:
        args = key_args[name]
    else:
        args = list(input_dict.keys())

    parts = []
    for arg in args:
        if arg in input_dict:
            val = input_dict[arg]
            if isinstance(val, str):
                if truncate_strings:
                    val = truncate(val, 60)
                parts.append(f'{arg}: "{val}"')
            elif isinstance(val, bool):
                parts.append(f"{arg}: {str(val).lower()}")
            elif isinstance(val, (int, float)):
                parts.append(f"{arg}: {val}")
            # Skip complex objects (lists, dicts)

    return ", ".join(parts)


def wrap_text(text, width, prefix, continuation_prefix):
    """Word-wrap text with a prefix on the first line and continuation indent."""
    if not text:
        return ""

    lines = text.split("\n")
    result = []
    for i, line in enumerate(lines):
        pfx = prefix if i == 0 and not result else continuation_prefix
        if not line.strip():
            result.append("")
            continue
        wrapped = textwrap.wrap(
            line, width=width - len(pfx), break_long_words=False, break_on_hyphens=False
        )
        if not wrapped:
            result.append("")
            continue
        for j, wline in enumerate(wrapped):
            if j == 0 and i == 0 and not result:
                result.append(prefix + wline)
            else:
                result.append(continuation_prefix + wline)
    return "\n".join(result)


def format_tool_result(content, width, max_lines=20):
    """Format a tool result with ⎿ prefix."""
    if not content:
        return "  ⎿  (empty)"

    # Normalize content
    if isinstance(content, list):
        # tool_result content can be a list of content blocks
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", item.get("content", str(item))))
            else:
                parts.append(str(item))
        content = "\n".join(parts)

    lines = content.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"  ... ({len(lines) - max_lines} more lines)"]

    result = []
    indent = "  ⎿  "
    cont_indent = "     "
    for i, line in enumerate(lines):
        pfx = indent if i == 0 else cont_indent
        # Truncate very long lines
        if len(line) > width - len(pfx):
            line = line[: width - len(pfx) - 3] + "..."
        result.append(pfx + line)
    return "\n".join(result)


def is_compaction_summary(content):
    """Check if a user message is a compaction summary."""
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                text = item
                break
    return text.startswith("This session is being continued from a previous conversation")


def format_timestamp(ts):
    """Format an ISO timestamp as a short local time string."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%d %b %H:%M:%S")
    except (ValueError, TypeError):
        return ""


def process_events(events, width, show_thinking=False, show_system=False,
                   show_tools=False, show_tool_output=False, show_compaction=False,
                   show_timestamps=False):
    """Process events and yield formatted output lines."""

    # Deduplicate: streaming sends multiple events per message ID.
    # Collect all content blocks per assistant message ID, in order.
    # Process user messages immediately (they aren't streamed).

    # First pass: collect all events, merging assistant content by message ID
    merged = []  # list of (type, data, timestamp) tuples
    assistant_msgs = {}  # msg_id -> index in merged list
    seen_content_ids = {}  # msg_id -> set of content block IDs/indices seen
    last_custom_title = None

    for event in events:
        etype = event.get("type")
        ts = event.get("timestamp", "")

        if etype == "user":
            msg = event.get("message", {})
            content = msg.get("content", "")
            # Skip task-notification system messages injected as user events
            content_str = content if isinstance(content, str) else ""
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        content_str = item
                        break
                    elif isinstance(item, dict) and item.get("type") == "text":
                        content_str = item.get("text", "")
                        break
            if "<task-notification>" in content_str:
                continue
            if not show_compaction and is_compaction_summary(content):
                merged.append(("compaction_marker", None, ts))
            else:
                merged.append(("user", content, ts))

        elif etype == "assistant":
            msg = event.get("message", {})
            msg_id = msg.get("id", "")
            content_blocks = msg.get("content", [])

            if msg_id and msg_id in assistant_msgs:
                # Append new content blocks to existing entry
                idx = assistant_msgs[msg_id]
                existing = merged[idx][1]
                seen = seen_content_ids[msg_id]
                for block in content_blocks:
                    # Deduplicate by block id or content
                    block_key = block.get("id") if isinstance(block, dict) else None
                    if block_key and block_key in seen:
                        # Update existing block (streaming may refine it)
                        for i, eb in enumerate(existing):
                            if isinstance(eb, dict) and eb.get("id") == block_key:
                                existing[i] = block
                                break
                    else:
                        if block_key:
                            seen.add(block_key)
                        existing.append(block)
            else:
                blocks = list(content_blocks)
                merged.append(("assistant", blocks, ts))
                if msg_id:
                    assistant_msgs[msg_id] = len(merged) - 1
                    seen_content_ids[msg_id] = set()
                    for b in blocks:
                        if isinstance(b, dict) and b.get("id"):
                            seen_content_ids[msg_id].add(b["id"])

        elif etype == "custom-title":
            title = event.get("customTitle", "")
            if title != last_custom_title:
                last_custom_title = title
                merged.append(("custom_title", title, ts))

        elif etype == "system" and show_system:
            msg = event.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c if isinstance(c, str) else c.get("text", "") for c in content
                )
            else:
                text = str(content)
            if text.strip():
                merged.append(("system", text, ts))

    # Second pass: render
    for entry_type, data, ts in merged:
        ts_prefix = f"[{format_timestamp(ts)}] " if show_timestamps and ts else ""

        if entry_type == "user":
            lines = list(render_user(data, width, show_tool_output))
            if any(l.strip() for l in lines):
                if ts_prefix and lines:
                    lines[0] = ts_prefix + lines[0]
                yield from lines
                yield ""

        elif entry_type == "assistant":
            lines = list(render_assistant(data, width, show_thinking, show_tools))
            if any(l.strip() for l in lines):
                if ts_prefix and lines:
                    lines[0] = ts_prefix + lines[0]
                yield from lines
                yield ""

        elif entry_type == "custom_title":
            yield f'{ts_prefix}— renamed to "{data}" —'
            yield ""

        elif entry_type == "compaction_marker":
            yield f"{ts_prefix}--- (compaction summary omitted, use --compaction to show) ---"
            yield ""

        elif entry_type == "system":
            line = wrap_text(data, width, "⚙ ", "  ")
            if ts_prefix:
                line = ts_prefix + line
            yield line
            yield ""


def render_user(content, width, show_tool_output=False):
    """Render a user message."""
    if isinstance(content, str):
        yield wrap_text(content, width, "❯ ", "  ")
        return

    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                yield wrap_text(item, width, "❯ ", "  ")
            elif isinstance(item, dict):
                if item.get("type") == "tool_result":
                    if show_tool_output:
                        result_content = item.get("content", "")
                        yield format_tool_result(result_content, width)
                elif item.get("type") == "text":
                    yield wrap_text(item.get("text", ""), width, "❯ ", "  ")
            # Skip other content types (images, etc.)


def render_assistant(content_blocks, width, show_thinking=False, show_tools=False):
    """Render an assistant message's content blocks."""
    for block in content_blocks:
        if not isinstance(block, dict):
            continue

        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "").strip()
            if text:
                yield wrap_text(text, width, "● ", "  ")

        elif btype == "thinking" and show_thinking:
            text = block.get("thinking", "").strip()
            if text:
                # Show thinking in a dimmer style
                yield wrap_text(f"[thinking] {text}", width, "◌ ", "  ")

        elif btype == "tool_use" and show_tools:
            name = block.get("name", "unknown")
            args = format_tool_args(name, block.get("input", {}), truncate_strings=False)
            yield wrap_text(f"{name}({args})", width, "● ", "  ")


_script_dir = os.path.dirname(os.path.realpath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
from project_dir import PROJECT_DIR as _PROJECT_DIR, DB_PATH as TASK_DB, cwd_to_bucket

# Derive sessions dir from the project owner's home, not the running user's.
# This allows other users to view sessions when TASK_RUNNER_PROJECT is set
# (export_sessions.py makes those files world-readable on --backup). Falls
# back to the running user's home when the project dir doesn't exist or its
# owner can't be resolved — covers the no-task-runner-setup case.
def _resolve_project_owner_home():
    try:
        return os.path.expanduser("~" + pathlib.Path(_PROJECT_DIR).owner())
    except (OSError, KeyError):
        return os.path.expanduser("~")


_project_owner_home = _resolve_project_owner_home()
# Claude Code shards session JSONLs by cwd: each cwd gets its own
# subdirectory under PROJECTS_ROOT, named by replacing each "/" in the
# absolute cwd with "-". `--list` walks every bucket so cross-cwd
# sessions appear; SESSIONS_DIR is kept as the default bucket for
# legacy callers and as the fallback when project_dir isn't recorded.
PROJECTS_ROOT = os.path.join(_project_owner_home, ".claude", "projects")
DEFAULT_BUCKET = cwd_to_bucket(_project_owner_home)
SESSIONS_DIR = os.path.join(PROJECTS_ROOT, DEFAULT_BUCKET)


def _list_session_buckets():
    """Yield (bucket_name, bucket_path) for every project bucket on disk."""
    if not os.path.isdir(PROJECTS_ROOT):
        return
    for name in os.listdir(PROJECTS_ROOT):
        path = os.path.join(PROJECTS_ROOT, name)
        if os.path.isdir(path):
            yield name, path


def _format_cwd(cwd, project_dir):
    """Short display form for a session's working directory.

    Prefers the cwd captured from the session's events (unambiguous);
    falls back to the bucket basename (lossy — `-` could be either a
    real `-` or a `/` separator)."""
    if cwd:
        home = _project_owner_home
        if cwd == home:
            return "~"
        if cwd.startswith(home + "/"):
            return "~/" + cwd[len(home) + 1:]
        return cwd
    if project_dir:
        return project_dir
    return ""
# Hardlink backups created by export_sessions.py / `task_runner.py --backup`.
# When a session file is vacuumed from SESSIONS_DIR, the hardlink here keeps
# the data alive and lets us still show / view the session.
BACKUP_SESSIONS_DIR = os.path.join(_PROJECT_DIR, "sessions")
# Legacy prefix for detecting old task sessions (before DB tracking).
# New sessions are detected via session_id in the runs table.
TASK_PROMPT_PREFIX = "You are working on the minimal associated primes computation project."

SESSIONS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    file_mtime REAL,
    is_task INTEGER DEFAULT 0,
    is_chat INTEGER DEFAULT 0,
    has_messages INTEGER DEFAULT 0,
    custom_title TEXT,
    display_name TEXT,
    first_ts TEXT,
    last_ts TEXT,
    user_msg_count INTEGER DEFAULT 0,
    first_user_msg TEXT,
    file_size INTEGER,
    deleted INTEGER DEFAULT 0,
    backup_path TEXT,
    project_dir TEXT,
    cwd TEXT
);
"""


def get_db():
    """Connect to tasks.db and ensure sessions table exists.

    If tasks.db doesn't exist, falls back to an in-memory database so the
    viewer still works for users who haven't set up the task runner. The
    cache then doesn't persist across calls (every --list re-parses every
    JSONL on disk) but the read paths are otherwise unchanged."""
    if os.path.isfile(TASK_DB):
        db = sqlite3.connect(TASK_DB)
    else:
        db = sqlite3.connect(":memory:")
    db.executescript(SESSIONS_TABLE_SCHEMA)
    # Migration: add backup_path to pre-existing sessions tables.
    cols = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
    for col, ddl in (
        ("backup_path", "ALTER TABLE sessions ADD COLUMN backup_path TEXT"),
        ("project_dir", "ALTER TABLE sessions ADD COLUMN project_dir TEXT"),
        ("cwd", "ALTER TABLE sessions ADD COLUMN cwd TEXT"),
    ):
        if col not in cols:
            try:
                db.execute(ddl)
                db.commit()
            except sqlite3.OperationalError:
                pass  # read-only DB
    return db


def safe_filename(name):
    """Convert a display name to the filename used by export_sessions.py."""
    name = name.replace("/", "-").replace("\\", "-")
    name = name.replace(":", "-").replace("*", "").replace("?", "")
    name = name.replace('"', "").replace("<", "").replace(">", "")
    name = name.replace("|", "-")
    name = re.sub(r"[-\s]+", "-", name)
    return name.strip("-")


def find_backup_path(display_name):
    """Return the hardlink backup path for a display_name, or None.

    Mirrors the naming used by export_sessions.py: <safe_filename>.jsonl
    under BACKUP_SESSIONS_DIR. Used to keep vacuumed sessions reachable.
    """
    if not display_name or not os.path.isdir(BACKUP_SESSIONS_DIR):
        return None
    p = os.path.join(BACKUP_SESSIONS_DIR, safe_filename(display_name) + ".jsonl")
    return p if os.path.isfile(p) else None


def get_task_session_ids():
    """Get session IDs known to be old-architecture task runner sessions.

    Only returns runs.session_id, which is set by the pre-Agent-tool
    `claude --print` architecture (top-level JSONL per task). New-arch
    tasks (agent_id set, session_id NULL) record to subagent JSONLs
    under `<bucket>/<parent>/subagents/`, never as top-level files, so
    scan_sessions never encounters them — no need to include agent_id
    here. Chat continuations from `task_runner.py --chat` are handled
    separately by `get_chat_session_ids` (they're their own category).
    """
    if not os.path.exists(TASK_DB):
        return set()
    db = sqlite3.connect(TASK_DB)
    ids = set(
        r[0] for r in db.execute(
            "SELECT DISTINCT session_id FROM runs WHERE session_id IS NOT NULL"
        )
    )
    db.close()
    return ids


def get_chat_session_ids():
    """Get session IDs that are chat continuations from `task_runner.py --chat`.

    These are top-level JSONLs created by copying a subagent log and
    resuming via `claude --resume`. They start with the task prompt
    (so the prefix heuristic in `is_task_session` would misclassify
    them as tasks), but they belong to their own category: not a task
    run, but not a freestanding interactive session either.
    """
    if not os.path.exists(TASK_DB):
        return set()
    db = sqlite3.connect(TASK_DB)
    ids = set(
        r[0] for r in db.execute(
            "SELECT DISTINCT chat_session_id FROM runs WHERE chat_session_id IS NOT NULL"
        )
    )
    db.close()
    return ids


def is_task_session(path, task_session_ids):
    """Check if a session file is a task runner session."""
    sid = os.path.basename(path).replace(".jsonl", "")
    if sid in task_session_ids:
        return True
    # Fall back to checking first user message for task prompt prefix
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("type") == "user":
                    content = event.get("message", {}).get("content", "")
                    if isinstance(content, str) and content.startswith(TASK_PROMPT_PREFIX):
                        return True
                    return False
    except (json.JSONDecodeError, OSError):
        pass
    return False


def get_session_info(path):
    """Extract metadata from a session jsonl file."""
    first_ts = None
    last_ts = None
    first_user_msg = None
    event_count = 0
    user_msg_count = 0
    assistant_msg_count = 0

    custom_title = None
    cwd = None

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_count += 1
                ts = event.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                if cwd is None:
                    ev_cwd = event.get("cwd")
                    if isinstance(ev_cwd, str) and ev_cwd:
                        cwd = ev_cwd

                etype = event.get("type")
                if etype == "custom-title":
                    custom_title = event.get("customTitle")
                elif etype == "user":
                    msg = event.get("message", {})
                    content = msg.get("content", "")
                    # Count user text messages (not tool results)
                    if isinstance(content, str) and content.strip():
                        user_msg_count += 1
                        if first_user_msg is None:
                            first_user_msg = content.strip()
                    elif isinstance(content, list):
                        has_text = any(
                            isinstance(item, str) and item.strip()
                            for item in content
                        )
                        if has_text:
                            user_msg_count += 1
                        if first_user_msg is None:
                            for item in content:
                                if isinstance(item, str) and item.strip():
                                    first_user_msg = item.strip()
                                    break
                elif etype == "assistant":
                    # Count unique assistant messages (deduplicate by msg id)
                    assistant_msg_count += 1
    except OSError:
        pass

    return {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "first_user_msg": first_user_msg,
        "event_count": event_count,
        "user_msg_count": user_msg_count,
        "custom_title": custom_title,
        "cwd": cwd,
        "size": os.path.getsize(path),
    }


def scan_sessions(db):
    """Scan session files and update the sessions cache table.

    Walks every bucket under PROJECTS_ROOT (Claude Code shards sessions
    by cwd), so sessions started outside ~/ also appear. Only re-parses
    files whose mtime has changed AND whose recorded bucket matches.
    Preserves display_name on re-scan. Marks sessions whose files no
    longer exist (in any bucket) as deleted. Silently skips cache
    updates if the database is read-only.
    """
    if not os.path.isdir(PROJECTS_ROOT):
        return

    # Check if database is writable (may be read-only for other users)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS _write_test (x INTEGER)")
        db.execute("DROP TABLE IF EXISTS _write_test")
    except sqlite3.OperationalError:
        return  # read-only — skip cache update, use existing data

    task_ids = get_task_session_ids()
    chat_ids = get_chat_session_ids()

    # Get current cached mtimes, display_names, and recorded buckets
    cached = {}
    for row in db.execute(
        "SELECT session_id, file_mtime, display_name, project_dir FROM sessions"
    ):
        cached[row[0]] = {
            "mtime": row[1],
            "display_name": row[2],
            "project_dir": row[3],
        }

    # Scan all jsonl files across every bucket
    disk_sids = set()
    for bucket_name, bucket_path in _list_session_buckets():
        try:
            entries = os.listdir(bucket_path)
        except OSError:
            continue
        for f in entries:
            if not f.endswith(".jsonl"):
                continue
            sid = f.replace(".jsonl", "")
            disk_sids.add(sid)
            path = os.path.join(bucket_path, f)

            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue

            # Skip if mtime hasn't changed AND we already recorded this
            # bucket. The bucket check catches sessions that moved between
            # buckets without their mtime changing (rare but possible).
            if (
                sid in cached
                and cached[sid]["mtime"] == mtime
                and cached[sid].get("project_dir") == bucket_name
            ):
                db.execute("UPDATE sessions SET deleted = 0 WHERE session_id = ? AND deleted = 1", (sid,))
                continue

            # Parse the file. Classification is three-way:
            #   chat-id match -> is_chat=1 (its own category — preserved
            #                    by export_sessions, hidden from --list)
            #   else, task-id or prefix match -> is_task=1
            #   else -> interactive (is_task=0, is_chat=0)
            info = get_session_info(path)
            if sid in chat_ids:
                is_task, is_chat = 0, 1
            elif is_task_session(path, task_ids):
                is_task, is_chat = 1, 0
            else:
                is_task, is_chat = 0, 0
            has_msgs = 1 if info["user_msg_count"] > 0 else 0

            # Preserve display_name on re-scan
            display_name = cached[sid]["display_name"] if sid in cached else None

            db.execute("""
                INSERT OR REPLACE INTO sessions
                    (session_id, file_mtime, is_task, is_chat, has_messages,
                     custom_title, display_name, first_ts, last_ts,
                     user_msg_count, first_user_msg, file_size, deleted,
                     project_dir, cwd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """, (sid, mtime, is_task, is_chat, has_msgs, info["custom_title"],
                  display_name, info["first_ts"], info["last_ts"],
                  info["user_msg_count"], info["first_user_msg"],
                  info["size"], bucket_name, info["cwd"]))

    # Sessions whose live files are gone: keep them visible if a backup
    # hardlink under BACKUP_SESSIONS_DIR survived (export_sessions.py's
    # output). Otherwise mark deleted.
    for sid in cached:
        if sid in disk_sids:
            continue
        backup = find_backup_path(cached[sid]["display_name"])
        if backup:
            db.execute(
                "UPDATE sessions SET deleted = 0, backup_path = ? WHERE session_id = ?",
                (backup, sid),
            )
        else:
            db.execute("UPDATE sessions SET deleted = 1 WHERE session_id = ?", (sid,))

    # Idempotent reclassification: mtime-cached rows weren't re-run
    # through the per-file classifier above, so chat-continuation IDs
    # that were misclassified is_task=1 (via the prefix heuristic in
    # earlier versions) get corrected here on every scan.
    db.execute("""
        UPDATE sessions SET is_chat = 1, is_task = 0
        WHERE session_id IN (
            SELECT chat_session_id FROM runs WHERE chat_session_id IS NOT NULL
        ) AND (is_chat = 0 OR is_task = 1)
    """)

    db.commit()


def list_sessions(show_deleted=False, show_cwd=False):
    """List interactive (non-task) sessions from the cache."""
    if not os.path.isdir(PROJECTS_ROOT):
        print(f"Projects root not found: {PROJECTS_ROOT}", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    scan_sessions(db)

    query = """
        SELECT session_id, first_ts, file_size, user_msg_count,
               display_name, custom_title, first_user_msg, deleted,
               project_dir, cwd
        FROM sessions
        WHERE is_task = 0 AND is_chat = 0 AND has_messages = 1
    """
    if not show_deleted:
        query += " AND deleted = 0"
    query += " ORDER BY first_ts DESC"

    rows = db.execute(query).fetchall()
    db.close()

    if not rows:
        print("No interactive sessions found.")
        return

    cwd_w = 24
    if show_cwd:
        print(f"{'Date':<18}  {'Size':>6}  {'Msgs':>4}  {'Cwd':<{cwd_w}}  {'Name':<20}  First message")
        print(f"{'─' * 18}  {'─' * 6}  {'─' * 4}  {'─' * cwd_w}  {'─' * 20}  {'─' * 40}")
    else:
        print(f"{'Date':<18}  {'Size':>6}  {'Msgs':>4}  {'Name':<20}  First message")
        print(f"{'─' * 18}  {'─' * 6}  {'─' * 4}  {'─' * 20}  {'─' * 40}")
    for (sid, first_ts, file_size, user_msg_count, display_name, custom_title,
         first_user_msg, deleted, project_dir, cwd) in rows:
        ts = first_ts or ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts = dt.strftime("%d %b %Y %H:%M")
            except ValueError:
                ts = ts[:16]

        size_kb = (file_size or 0) // 1024
        if size_kb >= 1024:
            size_str = f"{size_kb / 1024:.1f}M"
        else:
            size_str = f"{size_kb}K"

        name = display_name or custom_title or sid[:20]
        if deleted:
            name = f"{name} [deleted]"

        preview = first_user_msg or "(empty)"
        max_preview = 60
        if len(preview) > max_preview:
            preview = preview[:max_preview - 3] + "..."
        preview = preview.replace("\n", " ")

        if show_cwd:
            cwd_disp = _format_cwd(cwd, project_dir)
            if len(cwd_disp) > cwd_w:
                cwd_disp = cwd_disp[:cwd_w - 1] + "…"
            print(f"{ts:<18}  {size_str:>6}  {user_msg_count:>4}  {cwd_disp:<{cwd_w}}  {name:<20}  {preview}")
        else:
            print(f"{ts:<18}  {size_str:>6}  {user_msg_count:>4}  {name:<20}  {preview}")


def resolve_session(name):
    """Resolve a session name or ID prefix to a jsonl file path.

    Queries the sessions cache table for matches by display_name,
    custom_title (case-insensitive), or session_id prefix.
    Returns the path, or exits with an error.
    """
    if not os.path.isdir(PROJECTS_ROOT):
        print(f"Projects root not found: {PROJECTS_ROOT}", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    scan_sessions(db)

    # Query for matches: display_name, custom_title (case-insensitive), or ID prefix
    rows = db.execute("""
        SELECT session_id, display_name, custom_title, backup_path, project_dir
        FROM sessions
        WHERE deleted = 0 AND (
            display_name = ? COLLATE NOCASE
            OR custom_title = ? COLLATE NOCASE
            OR session_id LIKE ? || '%'
        )
    """, (name, name, name)).fetchall()
    db.close()

    if len(rows) == 1:
        sid, _, _, backup_path, project_dir = rows[0]
        bucket = project_dir or DEFAULT_BUCKET
        live = os.path.join(PROJECTS_ROOT, bucket, sid + ".jsonl")
        if os.path.isfile(live):
            return live
        if backup_path and os.path.isfile(backup_path):
            return backup_path
        return live  # caller will fail loudly with the live path
    elif len(rows) == 0:
        print(f"No session matching '{name}'", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Ambiguous session '{name}', matches:", file=sys.stderr)
        for sid, display_name, custom_title, _, _ in rows:
            label = display_name or custom_title or sid
            print(f"  {sid}  {label}", file=sys.stderr)
        sys.exit(1)


def set_display_name(session_ref, name):
    """Set or clear a display_name for a session."""
    if not os.path.isdir(PROJECTS_ROOT):
        print(f"Projects root not found: {PROJECTS_ROOT}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(TASK_DB):
        print(
            f"--name requires a task-runner database at {TASK_DB} "
            "to persist the name. Set TASK_RUNNER_PROJECT or run init_db.py.",
            file=sys.stderr,
        )
        sys.exit(1)

    db = get_db()
    scan_sessions(db)

    # Resolve the session
    rows = db.execute("""
        SELECT session_id, display_name, custom_title FROM sessions
        WHERE deleted = 0 AND (
            display_name = ? COLLATE NOCASE
            OR custom_title = ? COLLATE NOCASE
            OR session_id LIKE ? || '%'
        )
    """, (session_ref, session_ref, session_ref)).fetchall()

    if len(rows) == 0:
        print(f"No session matching '{session_ref}'", file=sys.stderr)
        db.close()
        sys.exit(1)
    elif len(rows) > 1:
        print(f"Ambiguous session '{session_ref}', matches:", file=sys.stderr)
        for sid, dn, ct in rows:
            label = dn or ct or sid
            print(f"  {sid}  {label}", file=sys.stderr)
        db.close()
        sys.exit(1)

    sid = rows[0][0]
    display_name = name if name else None

    db.execute("UPDATE sessions SET display_name = ? WHERE session_id = ?",
               (display_name, sid))
    db.commit()
    db.close()

    if display_name:
        print(f"Set display name for {sid[:8]}... to '{display_name}'")
    else:
        print(f"Cleared display name for {sid[:8]}...")


def main():
    parser = argparse.ArgumentParser(
        description="Format a Claude Code session jsonl file for readable output"
    )
    parser.add_argument("file", nargs="?", help="Path to session .jsonl file")
    parser.add_argument(
        "--list", action="store_true",
        help="List interactive sessions (not task runner sessions)"
    )
    parser.add_argument(
        "--deleted", action="store_true",
        help="Include deleted sessions in --list output"
    )
    parser.add_argument(
        "--cwd", action="store_true",
        help="Include each session's cwd column in --list output "
             "(useful for distinguishing sessions across project buckets)"
    )
    parser.add_argument(
        "--name", nargs=2, metavar=("SESSION", "NAME"),
        help="Set a display name for a session (use empty string to clear)"
    )
    parser.add_argument(
        "-w", "--width", type=int, default=0, help="Output width (default: terminal width)"
    )
    parser.add_argument(
        "--thinking", action="store_true", help="Show thinking blocks"
    )
    parser.add_argument(
        "--system", action="store_true", help="Show system messages"
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Verbosity level: -v shows tool calls, -vv also shows tool output"
    )
    parser.add_argument(
        "--tools", action="store_true",
        help="Show tool calls (hidden by default). Equivalent to -v."
    )
    parser.add_argument(
        "--tool-output", action="store_true",
        help="Show tool results (hidden by default; implies --tools). Equivalent to -vv."
    )
    parser.add_argument(
        "--tool-output-lines", type=int, default=20, metavar="N",
        help="Max lines per tool result (default: 20, 0=unlimited)"
    )
    parser.add_argument(
        "--compaction", action="store_true",
        help="Show compaction summaries (hidden by default)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Show everything (tools, tool output, thinking, system, compaction)"
    )
    parser.add_argument(
        "-t", "--timestamps", action="store_true",
        help="Show timestamps on each message"
    )
    args = parser.parse_args()

    if args.verbose >= 1:
        args.tools = True
    if args.verbose >= 2:
        args.tool_output = True

    if args.all:
        args.thinking = True
        args.system = True
        args.tools = True
        args.tool_output = True
        args.compaction = True

    if args.name:
        set_display_name(args.name[0], args.name[1])
        return

    if args.list:
        list_sessions(show_deleted=args.deleted, show_cwd=args.cwd)
        return

    if not args.file:
        parser.error("session name or file is required (or use --list)")

    # Resolve session name/ID if not a file path
    file_path = args.file
    if not os.path.isfile(file_path):
        file_path = resolve_session(args.file)

    width = args.width or get_terminal_width()

    # Read and parse all events
    events = []
    with open(file_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                events.append(event)
            except json.JSONDecodeError as e:
                print(f"Warning: skipping malformed JSON on line {line_num}: {e}", file=sys.stderr)

    # --tool-output implies --tools
    show_tools = args.tools or args.tool_output

    # Process and output
    for line in process_events(
        events, width,
        show_thinking=args.thinking,
        show_system=args.system,
        show_tools=show_tools,
        show_tool_output=args.tool_output,
        show_compaction=args.compaction,
        show_timestamps=args.timestamps,
    ):
        print(line)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    main()
