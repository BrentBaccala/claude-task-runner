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


def format_tool_args(name, input_dict):
    """Format tool_use input as a readable arg summary."""
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
            args = format_tool_args(name, block.get("input", {}))
            tool_line = f"● {name}({args})"
            if len(tool_line) > width:
                tool_line = tool_line[: width - 3] + "..."
            yield tool_line


SESSIONS_DIR = os.path.expanduser("~/.claude/projects/-home-claude")
_script_dir = os.path.dirname(os.path.realpath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
from project_dir import DB_PATH as TASK_DB
# Legacy prefix for detecting old task sessions (before DB tracking).
# New sessions are detected via session_id in the runs table.
TASK_PROMPT_PREFIX = "You are working on the minimal associated primes computation project."

SESSIONS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    file_mtime REAL,
    is_task INTEGER DEFAULT 0,
    has_messages INTEGER DEFAULT 0,
    custom_title TEXT,
    display_name TEXT,
    first_ts TEXT,
    last_ts TEXT,
    user_msg_count INTEGER DEFAULT 0,
    first_user_msg TEXT,
    file_size INTEGER,
    deleted INTEGER DEFAULT 0
);
"""


def get_db():
    """Connect to tasks.db and ensure sessions table exists."""
    db = sqlite3.connect(TASK_DB)
    db.executescript(SESSIONS_TABLE_SCHEMA)
    return db


def get_task_session_ids():
    """Get session IDs known to be task runner sessions from the DB."""
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
                event_count += 1
                ts = event.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

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
        "size": os.path.getsize(path),
    }


def scan_sessions(db):
    """Scan session files and update the sessions cache table.

    Only re-parses files whose mtime has changed. Preserves display_name
    on re-scan. Marks sessions whose files no longer exist as deleted.
    """
    if not os.path.isdir(SESSIONS_DIR):
        return

    task_ids = get_task_session_ids()

    # Get current cached mtimes and display_names
    cached = {}
    for row in db.execute("SELECT session_id, file_mtime, display_name FROM sessions"):
        cached[row[0]] = {"mtime": row[1], "display_name": row[2]}

    # Scan all jsonl files on disk
    disk_sids = set()
    for f in os.listdir(SESSIONS_DIR):
        if not f.endswith(".jsonl"):
            continue
        sid = f.replace(".jsonl", "")
        disk_sids.add(sid)
        path = os.path.join(SESSIONS_DIR, f)

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue

        # Skip if mtime hasn't changed
        if sid in cached and cached[sid]["mtime"] == mtime:
            # Un-delete if file reappeared
            db.execute("UPDATE sessions SET deleted = 0 WHERE session_id = ? AND deleted = 1", (sid,))
            continue

        # Parse the file
        info = get_session_info(path)
        is_task = 1 if is_task_session(path, task_ids) else 0
        has_msgs = 1 if info["user_msg_count"] > 0 else 0

        # Preserve display_name on re-scan
        display_name = cached[sid]["display_name"] if sid in cached else None

        db.execute("""
            INSERT OR REPLACE INTO sessions
                (session_id, file_mtime, is_task, has_messages, custom_title,
                 display_name, first_ts, last_ts, user_msg_count, first_user_msg,
                 file_size, deleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (sid, mtime, is_task, has_msgs, info["custom_title"],
              display_name, info["first_ts"], info["last_ts"],
              info["user_msg_count"], info["first_user_msg"],
              info["size"]))

    # Mark sessions whose files no longer exist
    for sid in cached:
        if sid not in disk_sids:
            db.execute("UPDATE sessions SET deleted = 1 WHERE session_id = ?", (sid,))

    db.commit()


def list_sessions(show_deleted=False):
    """List interactive (non-task) sessions from the cache."""
    if not os.path.isdir(SESSIONS_DIR):
        print(f"Sessions directory not found: {SESSIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    scan_sessions(db)

    query = """
        SELECT session_id, first_ts, file_size, user_msg_count,
               display_name, custom_title, first_user_msg, deleted
        FROM sessions
        WHERE is_task = 0 AND has_messages = 1
    """
    if not show_deleted:
        query += " AND deleted = 0"
    query += " ORDER BY first_ts DESC"

    rows = db.execute(query).fetchall()
    db.close()

    if not rows:
        print("No interactive sessions found.")
        return

    print(f"{'Date':<18}  {'Size':>6}  {'Msgs':>4}  {'Name':<20}  First message")
    print(f"{'─' * 18}  {'─' * 6}  {'─' * 4}  {'─' * 20}  {'─' * 40}")
    for sid, first_ts, file_size, user_msg_count, display_name, custom_title, first_user_msg, deleted in rows:
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

        print(f"{ts:<18}  {size_str:>6}  {user_msg_count:>4}  {name:<20}  {preview}")


def resolve_session(name):
    """Resolve a session name or ID prefix to a jsonl file path.

    Queries the sessions cache table for matches by display_name,
    custom_title (case-insensitive), or session_id prefix.
    Returns the path, or exits with an error.
    """
    if not os.path.isdir(SESSIONS_DIR):
        print(f"Sessions directory not found: {SESSIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    scan_sessions(db)

    # Query for matches: display_name, custom_title (case-insensitive), or ID prefix
    rows = db.execute("""
        SELECT session_id, display_name, custom_title FROM sessions
        WHERE deleted = 0 AND (
            display_name = ? COLLATE NOCASE
            OR custom_title = ? COLLATE NOCASE
            OR session_id LIKE ? || '%'
        )
    """, (name, name, name)).fetchall()
    db.close()

    if len(rows) == 1:
        sid = rows[0][0]
        return os.path.join(SESSIONS_DIR, sid + ".jsonl")
    elif len(rows) == 0:
        print(f"No session matching '{name}'", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Ambiguous session '{name}', matches:", file=sys.stderr)
        for sid, display_name, custom_title in rows:
            label = display_name or custom_title or sid
            print(f"  {sid}  {label}", file=sys.stderr)
        sys.exit(1)


def set_display_name(session_ref, name):
    """Set or clear a display_name for a session."""
    if not os.path.isdir(SESSIONS_DIR):
        print(f"Sessions directory not found: {SESSIONS_DIR}", file=sys.stderr)
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
        "--tools", action="store_true",
        help="Show tool calls (hidden by default)"
    )
    parser.add_argument(
        "--tool-output", action="store_true",
        help="Show tool results (hidden by default; implies --tools)"
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
        list_sessions(show_deleted=args.deleted)
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
