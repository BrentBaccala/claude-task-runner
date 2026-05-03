#!/usr/bin/env -S python3 -u
# NOTE: The -S flag is required for `env` to split "python3 -u" into two
# arguments. Without -S, env treats "python3 -u" as a single executable name.
# The -u flag disables Python's output buffering so log files are written in
# real-time and log output appears immediately instead of after the buffer fills.
"""
Task runner for the minimal associated primes project.

Manages a SQLite database of tasks with prompts, dependencies, and iterative
chains. Tasks are executed via the Claude Code Agent tool from within an
interactive Claude Code session — NOT via `claude --print` subprocesses.

Workflow:
    1. python3 task_runner.py --prepare NAME    # Mark running, output prompt
    2. Use the Claude Code Agent tool with the prompt
    3. python3 task_runner.py --complete NAME --result-status success [--result-value "N/M"]
       (pipe agent output via stdin to record it)

Other commands:
    python3 task_runner.py --list           # List all tasks and status
    python3 task_runner.py --status         # Show detailed status
    python3 task_runner.py --reset NAME     # Reset a failed/interrupted task to pending
    python3 task_runner.py --resume         # Reset all interrupted tasks to pending
    python3 task_runner.py --hold NAME      # Put a task on hold
    python3 task_runner.py --unhold NAME    # Remove hold, return to pending
    python3 task_runner.py --show NAME      # Show agent text only
    python3 task_runner.py --show NAME -v   # Also show tool invocations
    python3 task_runner.py --show NAME -vv  # Also show tool output
    python3 task_runner.py --log NAME       # Show formatted log of a task run
    python3 task_runner.py --kill NAME      # Mark a running task as interrupted
    python3 task_runner.py --continue NAME  # Set up a task for continuation
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")

# Add SCRIPT_DIR to path so we can import project_dir
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from project_dir import PROJECT_DIR, DB_PATH

LOGS_DIR = os.path.join(PROJECT_DIR, 'logs')

# Map agent types to Claude Code model flags.
# Any type not listed here defaults to "opus".
# Add new types as needed (e.g., "haiku": "haiku").
AGENT_MODELS = {
    "opus": "opus",
    "sonnet": "sonnet",
}




def has_progress(prev_value, new_value):
    """Compare result values between runs to detect improvement.

    Returns True if new_value represents progress over prev_value:
    - If both match N/M: compare numerators (10/11 → 11/11 = progress)
    - If both are numeric: compare as numbers (higher = progress)
    - If values differ at all: assume progress (conservative)
    - If identical: no progress
    """
    if prev_value is None or new_value is None:
        return prev_value is None and new_value is not None

    if prev_value == new_value:
        return False

    # Try N/M fraction comparison
    frac_re = r'^(\d+)\s*/\s*(\d+)$'
    prev_m = re.match(frac_re, prev_value)
    new_m = re.match(frac_re, new_value)
    if prev_m and new_m:
        return int(new_m.group(1)) > int(prev_m.group(1))

    # Try plain numeric comparison
    try:
        return float(new_value) > float(prev_value)
    except (ValueError, TypeError):
        pass

    # Values differ but aren't comparable — conservatively assume progress
    return True


def get_previous_result_value(db, task_id, current_run_id):
    """Get the result_value from the most recent prior run for this task."""
    row = db.execute(
        "SELECT result_value FROM runs "
        "WHERE task_id = ? AND id < ? AND result_value IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (task_id, current_run_id),
    ).fetchone()
    return row["result_value"] if row else None


def reset_chain(db, task):
    """Re-hold all fix tasks in a test task's chain.

    When rerun_after fires and resets a test task, the fix tasks should
    go back on hold so they're ready for the next iteration.
    """
    fix_name = task.get("on_partial_failure")
    if not fix_name:
        return

    # Hold the direct fix task (only if pending or failed; leave completed alone)
    db.execute(
        "UPDATE tasks SET status = 'hold', pending_context = NULL "
        "WHERE name = ? AND status IN ('pending', 'failed')",
        (fix_name,),
    )

    # Also hold any tasks whose rerun_after points to this test task's fix chain
    # Walk: fix task may have its own on_partial_failure, etc.
    visited = set()
    queue = [fix_name]
    while queue:
        fname = queue.pop(0)
        if fname in visited:
            continue
        visited.add(fname)
        # Find tasks that have rerun_after pointing to fname
        dependents = db.execute(
            "SELECT name, on_partial_failure FROM tasks WHERE rerun_after = ?",
            (fname,),
        ).fetchall()
        for dep in dependents:
            db.execute(
                "UPDATE tasks SET status = 'hold', pending_context = NULL "
                "WHERE name = ? AND status IN ('pending', 'failed')",
                (dep["name"],),
            )
            if dep["on_partial_failure"]:
                queue.append(dep["on_partial_failure"])

    db.commit()


def handle_failure(db, task, error_reason, run_id):
    """Handle a failed task via on_partial_failure chains.

    When a task reports TASK_RESULT: FAILURE with a result_value and has
    on_partial_failure set, activates the fix task if progress is being made.

    Returns the name of the activated task, or None if no action taken.
    """
    opf_target = task.get("on_partial_failure")
    if not opf_target or error_reason != "agent reported task failure":
        return None

    # Get the result_value from this run
    run_row = db.execute("SELECT result_value FROM runs WHERE id = ?", (run_id,)).fetchone()
    new_value = run_row["result_value"] if run_row else None

    if new_value is None:
        return None

    prev_value = get_previous_result_value(db, task["id"], run_id)
    iterate_count = task.get("iterate_count") or 0
    iterate_limit = task.get("iterate_limit") or 5

    # Check if we've hit the iteration limit
    if iterate_count >= iterate_limit:
        print(f"\n  → Iterate limit ({iterate_limit}) reached for {task['name']}")
        return None

    # Check for progress (first run always counts as progress)
    if prev_value is not None and not has_progress(prev_value, new_value):
        print(f"\n  → No progress ({prev_value} → {new_value}), not activating {opf_target}")
        return None

    # Activate the fix task
    fix_task = db.execute("SELECT * FROM tasks WHERE name = ?", (opf_target,)).fetchone()
    if fix_task is None:
        print(f"\n  → on_partial_failure target '{opf_target}' not found")
        return None

    # Build context to prepend to the fix task's prompt
    context = (
        f"The test task \"{task['name']}\" reported: TASK_RESULT: FAILURE {new_value}\n"
    )
    if prev_value:
        context += f"Previous result was: {prev_value}\n"
    else:
        context += "This is the first run.\n"
    context += (
        f"\nReview the test output at:\n"
        f"  python3 ~/project/task_runner.py --show {task['name']}\n\n"
    )

    # Unhold the fix task and inject context
    db.execute(
        "UPDATE tasks SET status = 'pending', pending_context = ? WHERE name = ?",
        (context, opf_target),
    )
    # Increment iterate_count on the test task
    db.execute(
        "UPDATE tasks SET iterate_count = ? WHERE id = ?",
        (iterate_count + 1, task["id"]),
    )
    db.commit()
    print(f"\n  → Activated fix task: {opf_target} (iteration {iterate_count + 1}/{iterate_limit})")
    if prev_value:
        print(f"    Progress: {prev_value} → {new_value}")
    return opf_target


def discover_repos():
    """Find all git repos/worktrees under ~/."""
    home = os.path.expanduser("~")
    repos = {}

    # Scan ~ for git repos (directories with .git dir or file)
    for name in os.listdir(home):
        if name.startswith("."):
            continue
        path = os.path.join(home, name)
        if not os.path.isdir(path):
            continue
        if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git")):
            repos[f"~/{name}"] = path

    # Discover worktrees from each known repo
    for label, path in list(repos.items()):
        result = subprocess.run(["git", "-C", path, "worktree", "list", "--porcelain"],
                                capture_output=True, text=True)
        for line in result.stdout.split("\n"):
            if line.startswith("worktree "):
                wt_path = line[len("worktree "):]
                if wt_path.startswith(home) and wt_path != path:
                    wt_label = "~/" + os.path.relpath(wt_path, home)
                    repos[wt_label] = wt_path

    # Home repo itself
    if os.path.isdir(os.path.join(home, ".git")):
        repos["~"] = home

    return repos


def post_task_commit(db, run_id, task_name):
    """Auto-commit changes in all repos after a successful task. Record files and state.

    Only commits agent deliverables (new files, docs, logs) — NOT infrastructure
    files like task_runner.py, init_db.py, tasks.db, or agent-settings.json.
    Those should be committed manually.
    """
    repos = discover_repos()
    all_committed_files = []
    commit_state = {}

    # Files that should never be auto-committed (relative to repo root)
    AUTO_COMMIT_EXCLUDE = {
        "task_runner.py",
        "init_db.py",
        "tasks.db",
        "agent-settings.json",
    }

    # Directory prefixes that should never be auto-committed.
    # This catches build artifacts even when a repo's .gitignore doesn't.
    AUTO_COMMIT_EXCLUDE_PREFIXES = (
        "build/",
        "build-",
    )

    def should_exclude(filepath):
        """Check if a file should be excluded from auto-commit."""
        if filepath in AUTO_COMMIT_EXCLUDE:
            return True
        if filepath.startswith(AUTO_COMMIT_EXCLUDE_PREFIXES):
            return True
        return False

    def get_committable_files(path):
        """Get lists of changed and untracked files, excluding infrastructure."""
        files_to_add = []
        file_records = []

        # Modified tracked files
        diff = subprocess.run(["git", "-C", path, "diff", "--name-status", "HEAD"],
                              capture_output=True, text=True)
        for line in diff.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2 and not should_exclude(parts[1]):
                files_to_add.append(parts[1])
                file_records.append((parts[0], parts[1]))

        # Untracked files
        untracked = subprocess.run(["git", "-C", path, "ls-files", "--others", "--exclude-standard"],
                                   capture_output=True, text=True)
        for line in untracked.stdout.strip().split("\n"):
            if line and not should_exclude(line):
                files_to_add.append(line)
                file_records.append(("A", line))

        return files_to_add, file_records

    # First pass: commit changes in subrepos (not home)
    for label, path in sorted(repos.items()):
        if label == "~":
            continue  # Home repo last

        files_to_add, file_records = get_committable_files(path)
        if files_to_add:
            for record in file_records:
                all_committed_files.append({"repo": label, "status": record[0], "file": record[1]})
            subprocess.run(["git", "-C", path, "add", "--pathspec-from-file=-"],
                           input="\n".join(files_to_add), capture_output=True, text=True)
            subprocess.run(["git", "-C", path, "commit", "-m", f"task-runner: {task_name}"],
                           capture_output=True, text=True)

        # Record HEAD
        head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        commit_state[label] = head.stdout.strip()

    # Second pass: home repo (catches strays + submodule pointer updates)
    home_path = repos.get("~")
    if home_path:
        files_to_add, file_records = get_committable_files(home_path)
        if files_to_add:
            for record in file_records:
                all_committed_files.append({"repo": "~", "status": record[0], "file": record[1]})
            subprocess.run(["git", "-C", home_path, "add", "--pathspec-from-file=-"],
                           input="\n".join(files_to_add), capture_output=True, text=True)
            subprocess.run(["git", "-C", home_path, "commit", "-m", f"task-runner: {task_name}"],
                           capture_output=True, text=True)

        head = subprocess.run(["git", "-C", home_path, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        commit_state["~"] = head.stdout.strip()

    # Store in DB
    db.execute("UPDATE runs SET committed_files = ?, commit_state = ? WHERE id = ?",
               (json.dumps(all_committed_files), json.dumps(commit_state), run_id))
    db.commit()

    if all_committed_files:
        print(f"\nAuto-committed {len(all_committed_files)} file(s) across {len(set(f['repo'] for f in all_committed_files))} repo(s)")


def commit_specific_files(db, run_id, task_name, files):
    """Commit specific files as artifacts of a task. Files are paths relative to ~/."""
    home = os.path.expanduser("~")
    repos = discover_repos()
    committed_files = []
    touched_repos = set()

    for filepath in files:
        # Resolve to absolute path
        abspath = os.path.abspath(os.path.join(home, filepath))
        if not os.path.exists(abspath):
            print(f"Warning: {filepath} does not exist, skipping")
            continue

        # Find which repo this file belongs to
        best_repo = None
        best_path = None
        best_label = None
        for label, repo_path in repos.items():
            if label == "~":
                continue
            if abspath.startswith(repo_path + "/"):
                if best_path is None or len(repo_path) > len(best_path):
                    best_repo = repo_path
                    best_path = repo_path
                    best_label = label

        # Fall back to home repo
        if best_repo is None and abspath.startswith(home + "/"):
            best_repo = home
            best_label = "~"

        if best_repo is None:
            print(f"Warning: {filepath} is not under any tracked repo, skipping")
            continue

        relfile = os.path.relpath(abspath, best_repo)
        subprocess.run(["git", "-C", best_repo, "add", relfile],
                       capture_output=True, text=True)
        committed_files.append({"repo": best_label, "status": "A", "file": relfile})
        touched_repos.add((best_label, best_repo))

    # Commit in each touched repo
    for label, repo_path in sorted(touched_repos):
        result = subprocess.run(
            ["git", "-C", repo_path, "commit", "-m", f"task-runner: {task_name}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"Committed in {label}")
        else:
            print(f"Warning: commit in {label} failed: {result.stderr.strip()}")

    # Record commit state for all repos
    commit_state = {}
    for label, repo_path in sorted(repos.items()):
        head = subprocess.run(["git", "-C", repo_path, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        commit_state[label] = head.stdout.strip()

    # Merge with any existing committed_files from this run
    existing = db.execute("SELECT committed_files FROM runs WHERE id = ?", (run_id,)).fetchone()
    if existing and existing["committed_files"]:
        prior = json.loads(existing["committed_files"])
        committed_files = prior + committed_files

    db.execute("UPDATE runs SET committed_files = ?, commit_state = ? WHERE id = ?",
               (json.dumps(committed_files), json.dumps(commit_state), run_id))
    db.commit()

    print(f"Recorded {len(files)} artifact(s) for task '{task_name}'")




def extract_result_stats(log_path):
    """Extract cost, turns, duration, and token usage from a stream-json log.

    A single claude --print invocation can contain multiple sessions
    (init → work → result cycles) due to context management or subagent
    notifications.  CC 2.1.69+ emits extra result events for sub-sessions
    (background agent completions) with only 1 turn and a few seconds.
    We use the first result's turns/duration (the main session) but the
    last result's cost (cumulative, includes subagent costs).

    When no result event exists (timeout, interrupt, crash), cost is unknown
    but token counts and turns are still extracted from per-message usage.
    """
    if not log_path or not os.path.exists(log_path):
        return None, None, None, None
    cost, turns, duration = None, None, None
    first_result_seen = False
    # Track per-message usage (dedup by message ID, keep last)
    msg_usage = {}
    first_ts, last_ts = None, None
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                cost = event.get("total_cost_usd", event.get("cost_usd"))
                if not first_result_seen:
                    # First result = main session: use its turns and duration
                    turns = event.get("num_turns")
                    duration = event.get("duration_ms")
                    first_result_seen = True
            elif event.get("type") == "assistant":
                msg = event.get("message", {})
                mid = msg.get("id")
                usage = msg.get("usage")
                if mid and usage:
                    msg_usage[mid] = usage
                ts = event.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

    # Compute token totals from per-message usage
    tokens = None
    if msg_usage:
        tokens = {
            "input": sum(u.get("input_tokens", 0) for u in msg_usage.values()),
            "output": sum(u.get("output_tokens", 0) for u in msg_usage.values()),
            "cache_read": sum(u.get("cache_read_input_tokens", 0) for u in msg_usage.values()),
            "cache_write": sum(u.get("cache_creation_input_tokens", 0) for u in msg_usage.values()),
        }
        if turns is None:
            turns = len(msg_usage)
    if duration is None and first_ts and last_ts:
        try:
            from datetime import datetime as _dt
            t0 = _dt.fromisoformat(first_ts)
            t1 = _dt.fromisoformat(last_ts)
            duration = int((t1 - t0).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass
    return cost, turns, duration, tokens


def extract_session_id(log_path):
    """Extract the last session_id from a stream-json log."""
    if not log_path or not os.path.exists(log_path):
        return None
    session_id = None
    with open(log_path) as f:
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
            if event.get("type") == "system" and event.get("subtype") == "init":
                session_id = event.get("session_id")
    return session_id


def ensure_str(value):
    """Decode bytes to str if needed (handles blob columns from SQLite)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def resolve_task_name(db, name_or_id):
    """Resolve a task name or numeric ID to a task name."""
    try:
        task_id = int(name_or_id)
        row = db.execute("SELECT name FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            return row["name"]
        print(f"Error: no task with id {task_id}")
        return None
    except ValueError:
        return name_or_id


def get_db():
    """Get a database connection, applying any pending schema migrations."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    _migrate(db)
    return db


def _migrate(db):
    """Apply schema migrations for new columns and tables (idempotent)."""
    # Inbox table for --send / --drain-inbox (hook-injected messages to running tasks)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS inbox (
        id INTEGER PRIMARY KEY,
        task_id INTEGER REFERENCES tasks(id),
        agent_id TEXT,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        delivered_at TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS inbox_agent_undelivered ON inbox(agent_id, delivered_at);
    CREATE INDEX IF NOT EXISTS inbox_task_undelivered ON inbox(task_id, delivered_at);
    """)

    # Collect existing columns for each table
    def has_column(table, column):
        cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        return column in cols

    migrations = [
        # (table, column, SQL to add it)
        ("runs", "result_status", "ALTER TABLE runs ADD COLUMN result_status TEXT"),
        ("runs", "result_value", "ALTER TABLE runs ADD COLUMN result_value TEXT"),
        ("tasks", "on_partial_failure", "ALTER TABLE tasks ADD COLUMN on_partial_failure TEXT"),
        ("tasks", "rerun_after", "ALTER TABLE tasks ADD COLUMN rerun_after TEXT"),
        ("tasks", "iterate_limit", "ALTER TABLE tasks ADD COLUMN iterate_limit INTEGER DEFAULT 5"),
        ("tasks", "iterate_count", "ALTER TABLE tasks ADD COLUMN iterate_count INTEGER DEFAULT 0"),
        ("tasks", "pending_context", "ALTER TABLE tasks ADD COLUMN pending_context TEXT"),
        ("tasks", "priority", "ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 10"),
        ("runs", "input_tokens", "ALTER TABLE runs ADD COLUMN input_tokens INTEGER"),
        ("runs", "output_tokens", "ALTER TABLE runs ADD COLUMN output_tokens INTEGER"),
        ("runs", "cache_read_tokens", "ALTER TABLE runs ADD COLUMN cache_read_tokens INTEGER"),
        ("runs", "cache_write_tokens", "ALTER TABLE runs ADD COLUMN cache_write_tokens INTEGER"),
        ("runs", "agent_id", "ALTER TABLE runs ADD COLUMN agent_id TEXT"),
        ("runs", "chat_session_id", "ALTER TABLE runs ADD COLUMN chat_session_id TEXT"),
        ("inbox", "session_id", "ALTER TABLE inbox ADD COLUMN session_id TEXT"),
    ]

    applied = 0
    for table, column, sql in migrations:
        if not has_column(table, column):
            db.execute(sql)
            applied += 1
    if applied:
        db.commit()


def show_summary(db):
    """Print aggregate statistics across all runs."""
    totals = db.execute("""
        SELECT
            count(*) as runs,
            sum(case when success then 1 else 0 end) as successes,
            sum(case when not success then 1 else 0 end) as failures,
            sum(duration_ms) as total_ms,
            sum(cost_usd) as total_cost,
            sum(num_turns) as total_turns
        FROM runs
    """).fetchone()

    task_counts = db.execute("""
        SELECT status, count(*) as cnt FROM tasks GROUP BY status ORDER BY status
    """).fetchall()

    by_agent = db.execute("""
        SELECT t.agent_type,
               count(*) as runs,
               sum(case when r.success then 1 else 0 end) as successes,
               sum(r.duration_ms) as total_ms,
               sum(r.cost_usd) as total_cost
        FROM runs r JOIN tasks t ON r.task_id = t.id
        GROUP BY t.agent_type ORDER BY total_cost DESC
    """).fetchall()

    total_ms = totals["total_ms"] or 0
    total_h = total_ms / 3_600_000
    total_cost = totals["total_cost"] or 0

    print("=== Task Summary ===\n")

    print("Tasks:")
    for row in task_counts:
        print(f"  {row['status']:12s} {row['cnt']:3d}")
    print()

    print(f"Runs:          {totals['runs']}")
    print(f"  Succeeded:   {totals['successes']}")
    print(f"  Failed:      {totals['failures']}")
    print(f"Total time:    {total_h:.1f} hours")
    print(f"Total cost:    ${total_cost:.2f}")
    print(f"Total turns:   {totals['total_turns']}")
    print()
    print(f"Default priority: 10 (higher runs first)")
    print()

    print(f"{'Agent':<14s} {'Model':<8s} {'Runs':>5s} {'OK':>4s} {'Time':>8s} {'Cost':>8s}")
    print("-" * 53)
    for row in by_agent:
        ms = row["total_ms"] or 0
        hours = ms / 3_600_000
        cost = row["total_cost"] or 0
        agent = row["agent_type"]
        model = AGENT_MODELS.get(agent, "opus")
        print(f"{agent:<14s} {model:<8s} {row['runs']:5d} {row['successes']:4d} {hours:7.1f}h ${cost:7.2f}")


def list_tasks(db):
    """Print a formatted task list."""
    tasks = db.execute(
        "SELECT id, name, status, agent_type, dependencies, completed_at, "
        "max_turns, timeout_seconds, on_partial_failure, rerun_after, "
        "iterate_count, iterate_limit, resume_session_id "
        "FROM tasks ORDER BY id"
    ).fetchall()

    name_w = max((len(t["name"]) for t in tasks), default=4)
    print(f"{'ID':>3}  {'Status':<12}  {'Agent':<12}  {'Name':<{name_w}}  Dependencies")
    print("-" * (35 + name_w + 20))
    for t in tasks:
        deps = json.loads(t["dependencies"])
        dep_str = ", ".join(deps) if deps else "-"
        flags = ""
        if t["max_turns"] is not None:
            flags += f" [max_turns={'unlimited' if t['max_turns'] == 0 else t['max_turns']}]"
        if t["timeout_seconds"] is not None:
            timeout_s = t["timeout_seconds"]
            if timeout_s == 0:
                flags += " [timeout=unlimited]"
            elif timeout_s >= 3600:
                h = timeout_s / 3600
                flags += f" [timeout={h:g}h]"
            else:
                m = timeout_s / 60
                flags += f" [timeout={m:g}m]"
        if t["resume_session_id"]:
            flags += " [continue]"
        if t["on_partial_failure"]:
            flags += f" [on_fail→{t['on_partial_failure']}]"
        if t["rerun_after"]:
            flags += f" [rerun→{t['rerun_after']}]"
        if t["iterate_count"]:
            flags += f" [iter {t['iterate_count']}/{t['iterate_limit']}]"
        print(f"{t['id']:>3}  {t['status']:<12}  {t['agent_type']:<12}  {t['name']:<{name_w}}  {dep_str}{flags}")


def list_history(db):
    """List tasks sorted by last run start time."""
    tasks = db.execute(
        "SELECT t.id, t.name, t.status, t.agent_type, "
        "  (SELECT MAX(r.started_at) FROM runs r WHERE r.task_id = t.id) as last_run, "
        "  (SELECT SUM(r.cost_usd) FROM runs r WHERE r.task_id = t.id) as cost, "
        "  (SELECT r.success FROM runs r WHERE r.task_id = t.id ORDER BY r.started_at DESC LIMIT 1) as last_success, "
        "  (SELECT r.pid FROM runs r WHERE r.task_id = t.id ORDER BY r.started_at DESC LIMIT 1) as pid, "
        "  (SELECT r.result_value FROM runs r WHERE r.task_id = t.id ORDER BY r.started_at DESC LIMIT 1) as result_value, "
        "  (SELECT SUM(r.output_tokens) FROM runs r WHERE r.task_id = t.id) as total_output_tokens "
        "FROM tasks t "
        "ORDER BY last_run IS NULL AND t.status != 'completed', last_run ASC, "
        "  t.status = 'completed' DESC"
    ).fetchall()

    print(f"{'ID':>3}  {'Status':<12}  {'Agent':<12}  {'Last Run':<12}  {'Cost':>8}  {'Out Tok':>9}  {'Result':<8}  Name")
    print("-" * 105)
    for t in tasks:
        last_run = t["last_run"] or "(never)"
        if t["last_run"]:
            # Shorten ISO timestamp to readable form
            try:
                dt = datetime.fromisoformat(t["last_run"])
                last_run = dt.strftime("%d %b %H:%M")
            except (ValueError, TypeError):
                pass
        cost = f"${t['cost']:.2f}" if t["cost"] is not None else ""
        tokens = f"{t['total_output_tokens']:,}" if t["total_output_tokens"] else ""
        result = t["result_value"] or ""
        suffix = f"  [pid {t['pid']}]" if t["status"] == "running" and t["pid"] else ""
        print(f"{t['id']:>3}  {t['status']:<12}  {t['agent_type']:<12}  {last_run:<12}  {cost:>8}  {tokens:>9}  {result:<8}  {t['name']}{suffix}")


def show_activity(db, limit=20):
    """Show recent activity across tasks, chat continuations, and interactive sessions."""
    events = []

    # Task runs (including chat continuations)
    runs = db.execute("""
        SELECT r.id as run_id, t.name, t.id as task_id, r.started_at, r.finished_at,
               r.success, r.result_value, r.cost_usd, r.chat_session_id, r.agent_id,
               r.id < MAX(r.id) OVER (PARTITION BY r.task_id) as has_later_run
        FROM runs r JOIN tasks t ON r.task_id = t.id
        WHERE r.started_at IS NOT NULL
        ORDER BY r.started_at DESC
    """).fetchall()

    for r in runs:
        started = r["started_at"]
        finished = r["finished_at"]
        status = "OK" if r["success"] else "FAIL"
        if not finished:
            if r["agent_id"]:
                status = "RUNNING"
            elif r["has_later_run"]:
                status = "STALE"
            else:
                status = "PREPARED"
        result = r["result_value"] or ""
        cost = f"${r['cost_usd']:.2f}" if r["cost_usd"] else ""
        name = r["name"]
        # Use finished_at for sorting (most recent activity), fall back to started_at
        sort_ts = finished or started
        events.append((sort_ts, "task", str(r["run_id"]), f"{status:<7s} {cost:>7s}  {result:<10s}  {name}"))

        # Chat continuation: show as separate entry if it exists
        if r["chat_session_id"]:
            # Find the last timestamp in the chat session
            chat_path = os.path.expanduser(
                f"~/.claude/projects/-home-claude/{r['chat_session_id']}.jsonl"
            )
            if os.path.exists(chat_path):
                last_ts = None
                try:
                    with open(chat_path, errors='replace') as f:
                        for line in f:
                            try:
                                ev = json.loads(line.strip())
                                if not isinstance(ev, dict):
                                    continue
                                ts = ev.get("timestamp", "")
                                if ts:
                                    last_ts = ts
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    pass
                if last_ts:
                    events.append((last_ts, "chat", "", f"{'':>7s} {'':>7s}  {'':>10s}  {name} (chat continuation)"))

    # Interactive sessions
    sessions = db.execute("""
        SELECT session_id, display_name, first_ts, last_ts, user_msg_count
        FROM sessions
        WHERE is_task = 0 AND has_messages = 1 AND deleted = 0
        ORDER BY last_ts DESC
    """).fetchall()

    for s in sessions:
        name = s["display_name"] or s["session_id"][:12]
        msgs = s["user_msg_count"] or 0
        sort_ts = s["last_ts"] or s["first_ts"] or ""
        events.append((sort_ts, "session", "", f"{'':>7s} {'':>7s}  {msgs:>3d} msgs    {name}"))

    # Sort by timestamp descending and truncate
    events.sort(key=lambda x: x[0] or "", reverse=True)
    events = events[:limit]

    # Format timestamps and print
    print(f"{'When':<14s}  {'Type':<7s}  {'Run':>4s}  {'Status':<7s} {'Cost':>7s}  {'Result':<10s}  Description")
    print("─" * 95)
    for ts, etype, run_str, detail in events:
        when = ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local_dt = dt.astimezone()
                when = local_dt.strftime("%-d %b %H:%M")
            except (ValueError, TypeError):
                when = ts[:16]
        print(f"{when:<14s}  {etype:<7s}  {run_str:>4s}  {detail}")


def show_status(db):
    """Show detailed status including dependency resolution."""
    tasks = {t["name"]: dict(t) for t in db.execute("SELECT * FROM tasks").fetchall()}

    print("=== Task Status ===\n")
    for name, t in tasks.items():
        deps = json.loads(t["dependencies"])
        accept_failure = t.get("run_on_dep_failure")
        finished_statuses = {"completed", "failed"} if accept_failure else {"completed"}
        blocked_by = []
        for dep_name in deps:
            dep = tasks.get(dep_name)
            if dep and dep["status"] not in finished_statuses:
                blocked_by.append(f"{dep_name} ({dep['status']})")

        ready = (
            t["status"] == "pending"
            and len(blocked_by) == 0
        )

        status_icon = {
            "pending": "○",
            "ready": "◐",
            "running": "◑",
            "completed": "●",
            "failed": "✗",
            "interrupted": "⚡",
            "hold": "⏸",
            "timeout": "⏱",
            "max_turns": "↻",
            "usage_limit": "⏱",
        }.get(t["status"], "?")

        print(f"{status_icon} [{t['status']}] {name}")
        if blocked_by:
            print(f"  Blocked by: {', '.join(blocked_by)}")
        if ready:
            print(f"  → READY to run")
        print(f"  Agent: {t['agent_type']}")
        print(f"  {t['description']}")
        print()

    # Show run history
    runs = db.execute(
        "SELECT r.*, t.name as task_name FROM runs r "
        "JOIN tasks t ON r.task_id = t.id ORDER BY r.started_at DESC LIMIT 10"
    ).fetchall()
    if runs:
        print("=== Recent Runs ===\n")
        for r in runs:
            success = "OK" if r["success"] else "FAIL"
            print(f"  {r['task_name']}: {success} at {r['started_at']}")
            if r["error_message"]:
                print(f"    Error: {r['error_message'][:100]}")


def get_ready_tasks(db):
    """Find pending tasks with all dependencies completed.

    Respects resource groups: if a task in the same resource_group is
    already running, the task is not considered ready. This prevents, e.g.,
    two `make -j4` builds from running simultaneously on a 4-core machine
    and thrashing. Tasks 2, 3, and 11 share the 'local-build' resource group.
    """
    tasks = db.execute("SELECT * FROM tasks").fetchall()
    task_map = {t["name"]: dict(t) for t in tasks}

    # Find resource groups that are currently busy
    busy_groups = set()
    for t in tasks:
        if t["status"] == "running" and t["resource_group"]:
            busy_groups.add(t["resource_group"])

    ready = []
    # Track which groups we've already added a task for in this batch,
    # so we don't schedule two from the same group in one pass
    scheduled_groups = set()

    for t in tasks:
        if t["status"] != "pending":
            continue
        deps = json.loads(t["dependencies"])
        accept_failure = t["run_on_dep_failure"]
        if accept_failure:
            finished_statuses = {"completed", "failed"}
            all_deps_done = all(
                task_map.get(dep, {}).get("status") in finished_statuses for dep in deps
            )
        else:
            all_deps_done = all(
                task_map.get(dep, {}).get("status") == "completed" for dep in deps
            )
        if not all_deps_done:
            continue
        group = t["resource_group"]
        if group and (group in busy_groups or group in scheduled_groups):
            continue
        ready.append(dict(t))
        if group:
            scheduled_groups.add(group)

    ready.sort(key=lambda t: t["priority"] if t.get("priority") is not None else 10, reverse=True)
    return ready


def reset_task(db, name):
    """Reset a failed, interrupted, timed-out, or max-turns task back to pending."""
    # Check for running task first — refuse to reset to avoid orphaning the active agent
    task = db.execute("SELECT status FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return False
    if task["status"] == "running":
        print(f"Error: task '{name}' is running — use --kill first")
        return False
    result = db.execute(
        "UPDATE tasks SET status = 'pending', completed_at = NULL, resume_session_id = NULL, pending_context = NULL "
        "WHERE name = ? AND status IN ('failed', 'interrupted', 'completed', 'timeout', 'max_turns', 'usage_limit', 'hold')",
        (name,),
    )
    if result.rowcount == 0:
        print(f"Error: task '{name}' is '{task['status']}', nothing to reset")
        return False
    db.commit()
    print(f"Reset: {name} → pending")
    return True


def resume_interrupted(db):
    """Reset all interrupted tasks back to pending."""
    result = db.execute(
        "UPDATE tasks SET status = 'pending', completed_at = NULL WHERE status = 'interrupted'"
    )
    db.commit()
    if result.rowcount == 0:
        print("No interrupted tasks to resume.")
    else:
        print(f"Resumed {result.rowcount} interrupted task(s) → pending")


def kill_task(db, name):
    """Mark a running task as interrupted.

    With Agent-tool execution, there is no subprocess to kill — the agent
    runs inside the Claude Code session. This command just updates the
    database status so the task can be re-prepared later.
    """
    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return False
    if task["status"] != "running":
        print(f"Error: task '{name}' is '{task['status']}', not running")
        return False

    finished_at = datetime.now().isoformat()

    # Update the latest run
    run = db.execute(
        "SELECT id FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    if run:
        db.execute(
            "UPDATE runs SET finished_at = ?, success = 0, error_message = ? WHERE id = ?",
            (finished_at, "marked interrupted by user", run["id"]),
        )
    db.execute(
        "UPDATE tasks SET status = 'interrupted' WHERE id = ?",
        (task["id"],),
    )
    db.commit()
    print(f"Task {name}: marked as INTERRUPTED")
    return True


def hold_task(db, name):
    """Put a task on hold so it won't be picked up by --prepare."""
    result = db.execute(
        "UPDATE tasks SET status = 'hold' WHERE name = ? AND status = 'pending'",
        (name,),
    )
    if result.rowcount == 0:
        task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
        if task is None:
            print(f"Error: task '{name}' not found")
        else:
            print(f"Error: task '{name}' is '{task['status']}', can only hold pending tasks")
        return False
    db.commit()
    print(f"Hold: {name}")
    return True


def unhold_task(db, name):
    """Remove hold from a task, returning it to pending."""
    result = db.execute(
        "UPDATE tasks SET status = 'pending' WHERE name = ? AND status = 'hold'",
        (name,),
    )
    if result.rowcount == 0:
        task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
        if task is None:
            print(f"Error: task '{name}' not found")
        else:
            print(f"Error: task '{name}' is '{task['status']}', not on hold")
        return False
    db.commit()
    print(f"Unhold: {name} → pending")
    return True


def show_task(db, name, verbosity=0, all_runs=False, timestamps=False):
    """Show task details, prompt, and run output."""
    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return

    print(f"Task: {task['name']} (id={task['id']})")
    print(f"Status: {task['status']}")
    print(f"Agent: {task['agent_type']}")
    print(f"Description: {task['description']}")
    deps = json.loads(task["dependencies"])
    if deps:
        print(f"Dependencies: {', '.join(deps)}")
    print(f"Deliverable: {task['deliverable_path'] or 'N/A'}")
    if task["resource_group"]:
        print(f"Resource group: {task['resource_group']}")
    if task["resume_session_id"]:
        print(f"Resume session: {task['resume_session_id']}")
    if task["pending_context"]:
        print(f"\n=== CONTINUATION PROMPT ===")
        print(task["pending_context"])
    print()
    print("=== PROMPT ===")
    prompt_path = os.path.join(PROJECT_DIR, "prompts", task["name"])
    if os.path.exists(prompt_path):
        with open(prompt_path) as f:
            print(f.read())
    else:
        print("(no prompt file found)")
    print()

    runs = db.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at",
        (task["id"],),
    ).fetchall()
    if not runs:
        print("(no runs yet)")
        return

    def format_run_header(r, label):
        if r["finished_at"] is None:
            status_tag = "RUNNING"
        elif r["success"]:
            status_tag = "OK"
            if r["result_value"]:
                status_tag = f"OK: {r['result_value']}"
        else:
            status_tag = "FAIL"
            if r["result_value"]:
                status_tag = f"FAIL: {r['result_value']}"
            elif r["error_message"]:
                status_tag = f"FAIL: {r['error_message']}"
        def fmt_ts(ts):
            if not ts:
                return "..."
            try:
                return datetime.fromisoformat(ts).strftime("%-d %b %H:%M")
            except (ValueError, TypeError):
                return ts
        header = f"=== RUN {label} [{status_tag}] {fmt_ts(r['started_at'])} → {fmt_ts(r['finished_at'])}"
        stats = []
        if r["cost_usd"] is not None:
            stats.append(f"${r['cost_usd']:.2f}")
        elif r["output_tokens"] is not None:
            stats.append(f"~{r['output_tokens']:,}tok out")
        if r["num_turns"] is not None:
            stats.append(f"{r['num_turns']} turns")
        if r["duration_ms"] is not None:
            stats.append(f"{r['duration_ms']/1000:.0f}s")
        if stats:
            header += f" ({', '.join(stats)})"
        header += " ==="
        if r["session_id"] and verbosity >= 1:
            header += f"\nSession: {r['session_id']}"
        return header

    def show_run_detail(r):
        """Show log, committed files, and analysis for a run.

        Verbosity levels (consistent for both subagent and old stream-json logs):
          0: assistant text only
          1: + tool invocations (which tools were called)
          2: + tool output (results from each tool)
          3: + full tool input content (Write bodies, Edit strings)

        For runs with a subagent log, we use that instead of agent_output
        (which is the Agent tool's raw return blob and can be very large).
        """
        subagent_log = None
        if r["agent_id"]:
            subagent_log = find_subagent_log(r["agent_id"])

        if subagent_log:
            print(format_log(subagent_log, verbosity=verbosity, timestamps=timestamps))
        else:
            log_path = r["log_path"]
            if log_path and os.path.exists(log_path):
                print(format_log(log_path, verbosity=verbosity, timestamps=timestamps))
            elif r["agent_output"]:
                print(r["agent_output"])

        if r["committed_files"]:
            files = json.loads(r["committed_files"])
            if files:
                print("\n=== Committed Files ===")
                current_repo = None
                for f in files:
                    if f["repo"] != current_repo:
                        current_repo = f["repo"]
                        print(f"  {current_repo}:")
                    print(f"    {f['status']} {f['file']}")

        analysis_log = subagent_log or (log_path if log_path and os.path.exists(log_path) else None)
        if analysis_log and verbosity >= 2:
            print("\n=== Session Analysis ===")
            print_log_analysis(analysis_log)

        if r["commit_state"] and verbosity >= 1:
            state = json.loads(r["commit_state"])
            print("\n=== Commit State ===")
            for repo, sha in sorted(state.items()):
                print(f"  {repo}: {sha[:12]}")

    def show_chat_continuation(r):
        """Show interactive --chat events that occurred after the task runner run."""

        def parse_ts(ts):
            """Parse a timestamp string, handling both local and UTC (Z suffix)."""
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.astimezone()
                return dt
            except (ValueError, AttributeError):
                return None

        # Find the session file and the cutoff timestamp
        session_path = None
        last_log_dt = None

        # New architecture: chat_session_id from --chat, cutoff from subagent log
        if r["chat_session_id"]:
            p = os.path.expanduser(f"~/.claude/projects/-home-claude/{r['chat_session_id']}.jsonl")
            if os.path.exists(p):
                session_path = p
            # Cutoff: last event in the subagent log
            if r["agent_id"]:
                subagent_log = find_subagent_log(r["agent_id"])
                if subagent_log:
                    with open(subagent_log, errors='replace') as f:
                        for line in f:
                            try:
                                ev = json.loads(line.strip())
                                if not isinstance(ev, dict):
                                    continue
                                ts = ev.get("timestamp")
                                if ts:
                                    dt = parse_ts(ts)
                                    if dt:
                                        last_log_dt = dt
                            except (json.JSONDecodeError, KeyError, ValueError):
                                pass

        # Old architecture: session_id from run, cutoff from stream-json log
        if not session_path:
            session_id = r["session_id"]
            if not session_id and r["log_path"]:
                session_id = extract_session_id(r["log_path"])
            if session_id:
                p = os.path.expanduser(f"~/.claude/projects/-home-claude/{session_id}.jsonl")
                if os.path.exists(p):
                    session_path = p
            if r["log_path"] and os.path.exists(r["log_path"]):
                with open(r["log_path"]) as f:
                    for line in f:
                        try:
                            ev = json.loads(line.strip())
                            if not isinstance(ev, dict):
                                continue
                            ts = ev.get("timestamp")
                            if ts:
                                dt = parse_ts(ts)
                                if dt:
                                    last_log_dt = dt
                        except (json.JSONDecodeError, KeyError, ValueError):
                            pass

        if not session_path or not last_log_dt:
            return

        # Collect events from session file that are after the cutoff
        chat_events = []
        with open(session_path, errors='replace') as f:
            for line in f:
                try:
                    ev = json.loads(line.strip())
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(ev, dict):
                    continue
                ev_dt = parse_ts(ev.get("timestamp", ""))
                if ev_dt and ev_dt > last_log_dt:
                    chat_events.append(ev)

        if not chat_events:
            return

        # Use format_session's process_events to render
        try:
            from format_session import process_events, get_terminal_width
            width = get_terminal_width()
            show_tools = verbosity >= 1
            show_tool_output = verbosity >= 2
            print("\n=== Chat Continuation ===")
            for line in process_events(
                chat_events, width,
                show_tools=show_tools,
                show_tool_output=show_tool_output,
                show_timestamps=timestamps,
            ):
                print(line)
        except ImportError:
            print("\n=== Chat Continuation ===")
            print(f"  ({len(chat_events)} events — install format_session.py to view)")

    if all_runs:
        for i, r in enumerate(runs, 1):
            print(format_run_header(r, i))
            print()
            show_run_detail(r)
            print()
        # Show chat continuation after the very last run
        show_chat_continuation(runs[-1])
    else:
        for i, r in enumerate(runs, 1):
            print(format_run_header(r, i))
        print()
        show_run_detail(runs[-1])
        show_chat_continuation(r)


def _format_ts(ts):
    """Convert an ISO timestamp to local time [YYYY-MM-DD HH:MM:SS] format."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return f"[{local_dt.strftime('%Y-%m-%d %H:%M:%S')}] "
    except (ValueError, TypeError):
        return ""


def format_stream_line(line, verbosity=0, timestamps=False):
    """Format a single stream-json line into readable text.

    verbosity=0: agent text and result summary only
    verbosity=1: also show tool invocations (which tools were called)
    verbosity=2: also show tool results (output from each tool)
    verbosity=3: also show full tool input content (Write content, Edit strings, etc)
    """
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(event, dict):
        return None

    etype = event.get("type", "")
    ts_prefix = _format_ts(event.get("timestamp")) if timestamps else ""

    if etype == "assistant":
        msg = event.get("message", {})
        parts = []
        for block in msg.get("content", []):
            if block.get("type") == "text":
                parts.append(block["text"])
            elif block.get("type") == "tool_use" and verbosity >= 1:
                tool = block.get("name", "?")
                inp = block.get("input", {})
                if tool == "Bash":
                    parts.append(f"[bash {inp.get('command', '')}]")
                elif tool == "Read":
                    parts.append(f"[read {inp.get('file_path', '?')}]")
                elif tool == "Write":
                    parts.append(f"[write {inp.get('file_path', '?')}]")
                    if verbosity >= 3:
                        content = inp.get("content", "")
                        if content:
                            parts.append(content)
                elif tool == "Edit":
                    parts.append(f"[edit {inp.get('file_path', '?')}]")
                    if verbosity >= 3:
                        old = inp.get("old_string", "")
                        new = inp.get("new_string", "")
                        if old or new:
                            parts.append(f"--- old ---\n{old}\n--- new ---\n{new}\n---")
                elif tool == "Glob":
                    parts.append(f"[glob {inp.get('pattern', '?')}]")
                elif tool == "Grep":
                    parts.append(f"[grep {inp.get('pattern', '?')}]")
                elif tool.startswith("mcp__"):
                    # MCP tools: mcp__server__method -> server.method
                    mcp_parts = tool.split("__", 2)
                    server = mcp_parts[1] if len(mcp_parts) > 1 else "?"
                    method = mcp_parts[2] if len(mcp_parts) > 2 else "?"
                    # Show key parameters depending on the method
                    detail = ""
                    if "command" in inp:
                        detail = f" {inp['command']}"
                    elif "cmd" in inp:
                        detail = f" {inp['cmd']}"
                    elif "hostAlias" in inp and "localPath" in inp:
                        detail = f" {inp.get('localPath', '?')} -> {inp.get('hostAlias', '?')}:{inp.get('remotePath', '?')}"
                    elif "hostAlias" in inp:
                        detail = f" {inp['hostAlias']}"
                    elif "location" in inp:
                        detail = f" {inp['location']}"
                    elif "expression" in inp:
                        detail = f" {inp['expression']}"
                    elif "function_call" in inp:
                        detail = f" {inp['function_call']}"
                    parts.append(f"[{server}.{method}{detail}]")
                    if verbosity >= 3 and inp:
                        # Show all parameters at -vvv
                        param_lines = [f"  {k}: {v}" for k, v in inp.items()]
                        parts.append("\n".join(param_lines))
                else:
                    parts.append(f"[{tool}]")
        return ts_prefix + "\n".join(parts) if parts else None

    elif etype == "user":
        if verbosity < 2:
            return None
        # Tool results
        msg = event.get("message", {})
        content = msg.get("content", [])
        # Session jsonl may have string content (initial prompt) — skip it
        if isinstance(content, str):
            return None
        parts = []
        for block in content:
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                # MCP tool results come as a list of text blocks
                if isinstance(content, list):
                    texts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text"]
                    content = "\n".join(texts)
                if isinstance(content, str) and content.strip():
                    if verbosity >= 3:
                        parts.append(content.strip())
                    else:
                        # Truncate long tool outputs
                        lines = content.strip().split("\n")
                        if len(lines) > 20:
                            parts.append("\n".join(lines[:10]))
                            parts.append(f"  ... ({len(lines) - 20} lines omitted) ...")
                            parts.append("\n".join(lines[-10:]))
                        else:
                            parts.append(content.strip())
        return ts_prefix + "\n".join(parts) if parts else None

    elif etype == "result":
        cost = event.get("total_cost_usd", event.get("cost_usd", 0))
        turns = event.get("num_turns", 0)
        duration = event.get("duration_ms", 0)
        subtype = event.get("subtype", "")
        # IMPORTANT: The result event contains a "result" field with text that
        # is an exact duplicate of the last assistant message's text. If we
        # display it, every run's output ends with the same text repeated twice.
        # We only extract the metadata (cost, turns, duration) here.
        return f"\n{ts_prefix}--- Result: {subtype} (turns={turns}, cost=${cost:.4f}, {duration/1000:.1f}s) ---"

    return None


def analyze_log_sessions(log_path):
    """Break down a stream-json log into sessions.

    A single claude --print invocation can contain multiple sessions
    (init → work → result cycles). This happens when Claude Code hits
    max turns, does context compaction, or restarts internally. Understanding
    the session structure helps diagnose unexpected behavior.

    Returns a list of session dicts with:
      - session_id, subtype (from init event)
      - num_turns, cost, duration_ms, result_subtype (from result event)
      - tool_calls: list of (tool_name, brief_info) for each tool use
      - assistant_messages: count of assistant text blocks
      - system_events: list of (subtype,) for non-init system events
    """
    sessions = []
    current = None

    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            ts = event.get("timestamp")  # injected by task_runner

            if etype == "system":
                subtype = event.get("subtype", "")
                if subtype == "init":
                    current = {
                        "session_id": event.get("session_id", "?"),
                        "model": event.get("model", ""),
                        "tool_calls": [],
                        "assistant_messages": 0,
                        "system_events": [],
                        "result_subtype": None,
                        "num_turns": None,
                        "cost": None,
                        "duration_ms": None,
                        "start_time": ts,
                        "end_time": None,
                    }
                    sessions.append(current)
                elif current:
                    current["system_events"].append(subtype)

            elif etype == "assistant" and current:
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        current["assistant_messages"] += 1
                    elif block.get("type") == "tool_use":
                        tool = block.get("name", "?")
                        inp = block.get("input", {})
                        if tool == "Bash":
                            # Extract a meaningful summary from the command,
                            # skipping cd prefixes and comment lines
                            cmd_text = inp.get("command", "")
                            cmd_line = ""
                            for cl in cmd_text.split("\n"):
                                cl = cl.strip()
                                if cl and not cl.startswith("#") and not cl.startswith("cd "):
                                    cmd_line = cl[:60]
                                    break
                            info = cmd_line or cmd_text[:60]
                        elif tool in ("Read", "Write", "Edit"):
                            info = inp.get("file_path", "?")
                        elif tool == "Glob":
                            info = inp.get("pattern", "?")
                        elif tool == "Grep":
                            info = inp.get("pattern", "?")
                        elif tool == "WebFetch":
                            info = inp.get("url", "?")[:60]
                        elif tool == "WebSearch":
                            info = inp.get("query", "?")[:60]
                        else:
                            info = ""
                        current["tool_calls"].append((tool, info, ts))

            elif etype == "user" and current and ts:
                # Tool results come back as user events — update the last
                # tool call's end time if we stored it
                current["end_time"] = ts

            elif etype == "result" and current:
                current["result_subtype"] = event.get("subtype", "")
                current["num_turns"] = event.get("num_turns")
                current["cost"] = event.get("total_cost_usd", event.get("cost_usd"))
                current["duration_ms"] = event.get("duration_ms")
                current["end_time"] = ts

    return sessions


def print_log_analysis(log_path):
    """Print a session-by-session breakdown of a log file."""
    sessions = analyze_log_sessions(log_path)
    if not sessions:
        print("  (no sessions found)")
        return

    print(f"  {len(sessions)} session(s) in this run:\n")
    for i, s in enumerate(sessions, 1):
        result = s["result_subtype"] or "no result"
        turns = s["num_turns"] or "?"
        cost = f"${s['cost']:.2f}" if s['cost'] else "?"
        duration = f"{s['duration_ms']/1000:.0f}s" if s['duration_ms'] else "?"
        tools = len(s["tool_calls"])
        texts = s["assistant_messages"]

        # Show wall-clock time range if timestamps are available
        time_range = ""
        if s.get("start_time") and s.get("end_time"):
            try:
                t0 = datetime.fromisoformat(s["start_time"])
                t1 = datetime.fromisoformat(s["end_time"])
                wall = (t1 - t0).total_seconds()
                time_range = f" [{t0.strftime('%H:%M:%S')}–{t1.strftime('%H:%M:%S')}, {wall:.0f}s wall]"
            except (ValueError, TypeError):
                pass
        elif s.get("start_time"):
            try:
                t0 = datetime.fromisoformat(s["start_time"])
                time_range = f" [started {t0.strftime('%H:%M:%S')}]"
            except (ValueError, TypeError):
                pass

        print(f"  Session {i}: {result} ({turns} turns, {cost} cumulative, {duration}){time_range}")
        print(f"    {texts} text messages, {tools} tool calls")

        if s["system_events"]:
            print(f"    system events: {', '.join(s['system_events'])}")

        # Show tool calls summary
        if s["tool_calls"]:
            for tool, info, ts in s["tool_calls"]:
                ts_str = ""
                if ts:
                    try:
                        t = datetime.fromisoformat(ts)
                        ts_str = f" [{t.strftime('%H:%M:%S')}]"
                    except (ValueError, TypeError):
                        pass
                if info:
                    print(f"      {tool}: {info}{ts_str}")
                else:
                    print(f"      {tool}{ts_str}")
        print()


def format_log(log_path, verbosity=0, timestamps=False):
    """Format an entire stream-json log file into readable text."""
    lines = []
    init_count = 0
    with open(log_path) as f:
        for line in f:
            # Track sub-sessions: background agent completions in CC 2.1.69+
            # emit extra init/result cycles that clutter output.  Suppress
            # the result events for sub-sessions (they just repeat TASK_RESULT).
            stripped = line.strip()
            if stripped:
                try:
                    ev = json.loads(stripped)
                    if isinstance(ev, dict):
                        if ev.get("type") == "system" and ev.get("subtype") == "init":
                            init_count += 1
                        elif ev.get("type") == "result" and init_count > 1:
                            continue
                except (json.JSONDecodeError, KeyError):
                    pass
            formatted = format_stream_line(line, verbosity=verbosity, timestamps=timestamps)
            if formatted:
                lines.append(formatted)
    return "\n".join(lines)




def log_task(db, name, verbosity=0, timestamps=False):
    """Show the formatted log of the most recent run."""
    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return
    run = db.execute(
        "SELECT log_path FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    log_path = run["log_path"] if run else None
    if not log_path or not os.path.exists(log_path):
        print(f"No log file for '{name}'")
        return
    print(f"Task: {name} (id={task['id']})")
    print(f"Status: {task['status']}")
    print(f"Log: {log_path}\n")
    print(format_log(log_path, verbosity=verbosity, timestamps=timestamps))


def set_agent_id(db, name, agent_id):
    """Record the Agent tool's agent ID on the most recent run.

    Called after the Agent tool returns, so --tail can find the subagent's
    live log file at ~/.claude/projects/.../subagents/agent-{agentId}.jsonl.
    """
    task = db.execute("SELECT id FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return False
    run = db.execute(
        "SELECT id FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    if run is None:
        print(f"Error: no run found for task '{name}'")
        return False
    db.execute("UPDATE runs SET agent_id = ? WHERE id = ?", (agent_id, run["id"]))
    # Mark the task as running now that an agent is actually executing
    db.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task["id"],))
    # Claim queued inbox messages for this task (set before an agent existed) for this agent.
    claimed = db.execute(
        "UPDATE inbox SET agent_id = ? "
        "WHERE task_id = ? AND agent_id IS NULL AND delivered_at IS NULL",
        (agent_id, task["id"]),
    ).rowcount
    db.commit()
    print(f"Recorded agent_id {agent_id} for task '{name}' (run {run['id']})")
    if claimed:
        print(f"Claimed {claimed} queued inbox message(s) for agent {agent_id}")
    return True


def send_message(db, name, message):
    """Queue a message for the agent running task `name`.

    If the task has a current run with an agent_id, the message is stamped
    with that agent_id and will be delivered on the next hook fire. Otherwise
    it is queued against task_id and `set_agent_id` will claim it later.
    """
    task = db.execute("SELECT id, status FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found", file=sys.stderr)
        return False
    # Only stamp agent_id from a live run (finished_at IS NULL). Otherwise queue
    # to task_id so the next run claims it via set_agent_id's backfill.
    agent_id = None
    if task["status"] == "running":
        run = db.execute(
            "SELECT agent_id FROM runs WHERE task_id = ? AND finished_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        if run and run["agent_id"]:
            agent_id = run["agent_id"]
    db.execute(
        "INSERT INTO inbox (task_id, agent_id, message) VALUES (?, ?, ?)",
        (task["id"], agent_id, message),
    )
    db.commit()
    if agent_id:
        print(f"Queued message for task '{name}' (agent {agent_id})")
    else:
        # Check for a chat session that could pick it up via drain_inbox's
        # session_id fallback. We only mention it if a claude process is
        # actually attached to that session — chat_task launches
        # `claude --resume <session_id>`, so matching the UUID in any
        # process's cmdline is reliable.
        chat_run = db.execute(
            "SELECT chat_session_id FROM runs WHERE task_id = ? "
            "AND chat_session_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        cs_active = False
        if chat_run:
            import psutil
            cs_id = chat_run["chat_session_id"]
            for p in psutil.process_iter(["cmdline"]):
                try:
                    if any(cs_id in arg for arg in (p.info["cmdline"] or [])):
                        cs_active = True
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        if cs_active:
            print(f"Queued message for task '{name}' "
                  f"(chat session {chat_run['chat_session_id'][:8]} will pick it up "
                  f"on its next turn)")
        else:
            print(f"Queued message for task '{name}' (will be claimed when agent launches)")
    return True


def resolve_session_id(db, ref):
    """Resolve a session reference to a full session UUID.

    Accepts: full UUID, session_id prefix, display_name, or custom_title
    (the last two are set via `format_session.py --name` or /rename in-chat).
    Returns the UUID string, or None on miss/ambiguity (error printed).
    """
    # Full UUID — accept as-is even if not in the sessions cache
    if re.fullmatch(r"[0-9a-fA-F-]{36}", ref):
        return ref

    # Refresh the sessions cache so recent renames are picked up
    try:
        import format_session
        format_session.scan_sessions(db)
    except ImportError:
        pass

    rows = db.execute("""
        SELECT session_id, display_name, custom_title FROM sessions
        WHERE deleted = 0 AND (
            display_name = ? COLLATE NOCASE
            OR custom_title = ? COLLATE NOCASE
            OR session_id LIKE ? || '%'
        )
    """, (ref, ref, ref)).fetchall()

    if len(rows) == 1:
        return rows[0][0]
    if len(rows) == 0:
        print(f"Error: no session matching '{ref}'", file=sys.stderr)
        return None
    print(f"Error: ambiguous session '{ref}', matches:", file=sys.stderr)
    for sid, display_name, custom_title in rows:
        label = display_name or custom_title or ""
        print(f"  {sid}  {label}", file=sys.stderr)
    return None


def send_session_message(db, session_id, message):
    """Queue a message for delivery to a specific Claude Code session.

    Delivered by drain_inbox when a hook fires with matching session_id.
    Not tied to any task.
    """
    db.execute(
        "INSERT INTO inbox (task_id, agent_id, session_id, message) "
        "VALUES (NULL, NULL, ?, ?)",
        (session_id, message),
    )
    db.commit()
    print(f"Queued message for session {session_id[:8]} (will be delivered on its next hook fire)")
    return True


def show_inbox(db, name=None):
    """Show inbox messages with delivery status.

    Without `name`: shows all messages across all tasks, most recent first.
    With `name`: shows only messages for that task.
    """
    if name is not None:
        task = db.execute("SELECT id FROM tasks WHERE name = ?", (name,)).fetchone()
        if task is None:
            print(f"Error: task '{name}' not found", file=sys.stderr)
            return False
        rows = db.execute(
            "SELECT i.id, i.task_id, i.agent_id, i.session_id, i.message, "
            "  i.created_at, i.delivered_at, t.name as task_name "
            "FROM inbox i LEFT JOIN tasks t ON t.id = i.task_id "
            "WHERE i.task_id = ? ORDER BY i.id DESC",
            (task["id"],),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT i.id, i.task_id, i.agent_id, i.session_id, i.message, "
            "  i.created_at, i.delivered_at, t.name as task_name "
            "FROM inbox i LEFT JOIN tasks t ON t.id = i.task_id "
            "ORDER BY i.id DESC"
        ).fetchall()

    if not rows:
        print("(no messages)")
        return True

    def fmt_ts(ts):
        if not ts:
            return ""
        try:
            return datetime.fromisoformat(ts).strftime("%d %b %H:%M")
        except (ValueError, TypeError):
            return ts

    for r in rows:
        status = f"delivered {fmt_ts(r['delivered_at'])}" if r["delivered_at"] else "queued"
        if r["task_name"]:
            target = f"task={r['task_name']}"
        elif r["session_id"]:
            target = f"session={r['session_id'][:8]}"
        else:
            target = "target=?"
        agent = r["agent_id"][:16] if r["agent_id"] else "-"
        print(f"#{r['id']}  {target}  sent={fmt_ts(r['created_at'])}  "
              f"agent={agent}  [{status}]")
        for line in r["message"].splitlines() or [""]:
            print(f"    {line}")
        print()
    return True


def drain_inbox(db):
    """Hook entry point. Reads hook JSON on stdin, emits queued messages.

    Two routing paths:
    - Subagent hooks: match inbox rows by `agent_id`.
    - Top-level interactive hooks: match the hook's `session_id` against
      `runs.chat_session_id`, then deliver unclaimed (agent_id IS NULL)
      inbox rows for that task. This covers `--chat` sessions where the
      user is conversing with a task's prior run outside a subagent.

    Output format depends on hook_event_name:
    - UserPromptSubmit / SessionStart: plain stdout is injected into context.
    - Everything else: emit JSON with hookSpecificOutput.additionalContext.
    """
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Nothing sensible to do; silently succeed so we don't block the turn.
        return True

    agent_id = data.get("agent_id")
    session_id = data.get("session_id")
    event = data.get("hook_event_name") or ""

    if agent_id:
        rows = db.execute(
            "SELECT id, message FROM inbox "
            "WHERE agent_id = ? AND delivered_at IS NULL ORDER BY id",
            (agent_id,),
        ).fetchall()
    elif session_id:
        # Direct session-targeted rows (from --send-session), plus any
        # task-keyed rows attached to a --chat session matching this session_id.
        task_row = db.execute(
            "SELECT task_id FROM runs WHERE chat_session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if task_row:
            rows = db.execute(
                "SELECT id, message FROM inbox "
                "WHERE delivered_at IS NULL AND ("
                "  session_id = ? OR "
                "  (task_id = ? AND agent_id IS NULL)"
                ") ORDER BY id",
                (session_id, task_row["task_id"]),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, message FROM inbox "
                "WHERE session_id = ? AND delivered_at IS NULL ORDER BY id",
                (session_id,),
            ).fetchall()
    else:
        return True

    if not rows:
        return True

    db.executemany(
        "UPDATE inbox SET delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
        [(r["id"],) for r in rows],
    )
    db.commit()

    body = "\n".join(r["message"] for r in rows)
    wrapped = f"<task-inbox>\n{body}\n</task-inbox>"

    if event in ("UserPromptSubmit", "SessionStart"):
        print(wrapped)
    else:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": wrapped,
            }
        }))
    return True


def find_subagent_log(agent_id):
    """Find the subagent's jsonl log file by agent ID.

    Searches ~/.claude/projects/*/subagents/agent-{agentId}.jsonl.
    Returns the path if found, None otherwise.
    """
    import glob
    pattern = os.path.expanduser(
        f"~/.claude/projects/*/subagents/agent-{agent_id}.jsonl"
    )
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
    # Also check one level deeper (session subdirectories)
    pattern = os.path.expanduser(
        f"~/.claude/projects/*/*/subagents/agent-{agent_id}.jsonl"
    )
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def tail_task(db, name, verbosity=0, timestamps=False):
    """Tail the live log of a running task's subagent.

    Watches the subagent's jsonl file at:
      ~/.claude/projects/.../subagents/agent-{agentId}.jsonl

    This file is written incrementally as the agent works, so we can
    tail it in real-time to see tool calls, results, and text output.
    """
    import time

    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return

    # Find the agent_id from the most recent run
    run = db.execute(
        "SELECT agent_id FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    agent_id = run["agent_id"] if run else None
    if not agent_id:
        print(f"Error: no agent_id recorded for task '{name}'")
        print("Use --set-agent-id NAME AGENT_ID after starting the Agent tool.")
        return

    log_path = find_subagent_log(agent_id)
    if not log_path:
        print(f"Error: subagent log not found for agent {agent_id}")
        print("The agent may not have started yet. Try again in a moment.")
        return

    print(f"Tailing {log_path} (Ctrl+C to stop)\n", flush=True)

    try:
        with open(log_path) as f:
            # Print existing content
            for line in f:
                formatted = format_stream_line(line, verbosity=verbosity, timestamps=timestamps)
                if formatted:
                    print(formatted, flush=True)

            # Tail new content
            while True:
                line = f.readline()
                if line:
                    formatted = format_stream_line(line, verbosity=verbosity, timestamps=timestamps)
                    if formatted:
                        print(formatted, flush=True)
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n(stopped tailing)")


def chat_task(db, name):
    """Open an interactive Claude session continuing a task's last agent run.

    Copies the subagent's jsonl log into a standalone session file
    (strips subagent markers but preserves original sessionIds) and
    launches `claude --resume` on it. This gives the user a full
    interactive session with the agent's complete conversation history.

    Preserving original sessionIds means cost_report can deduplicate:
    events whose sessionId doesn't match the filename are copied history
    and won't be double-counted.
    """
    import uuid as _uuid

    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return False

    # Find the most recent run with an agent_id
    run = db.execute(
        "SELECT id, agent_id, chat_session_id, session_id, log_path FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    agent_id = run["agent_id"] if run else None
    if not agent_id:
        # Fallback for old architecture: extract session_id from stream-json log
        log_path = run["log_path"] if run else None
        session_id = run["session_id"] if run else None
        if not session_id and log_path:
            session_id = extract_session_id(log_path)
        if session_id:
            session_path = os.path.expanduser(
                f"~/.claude/projects/-home-claude/{session_id}.jsonl"
            )
            if os.path.exists(session_path):
                print(f"Resuming old session {session_id}")
                os.chdir(os.path.expanduser("~"))
                os.execvp(CLAUDE_BIN, [CLAUDE_BIN, "--resume", session_id, "--dangerously-skip-permissions"])
            else:
                print(f"Error: session file not found: {session_path}")
                return False
        print(f"Error: no agent_id or session_id for task '{name}'")
        return False

    # If a chat session already exists for this run, resume it
    chat_session_id = run["chat_session_id"]
    if chat_session_id:
        chat_path = os.path.expanduser(
            f"~/.claude/projects/-home-claude/{chat_session_id}.jsonl"
        )
        if os.path.exists(chat_path):
            print(f"Resuming existing chat session {chat_session_id}")
            os.chdir(os.path.expanduser("~"))
            os.execvp(CLAUDE_BIN, [CLAUDE_BIN, "--resume", chat_session_id, "--dangerously-skip-permissions"])
        else:
            print(f"Warning: chat session file missing, creating new session")

    # Create a new chat session from the subagent log
    src_path = find_subagent_log(agent_id)
    if not src_path:
        print(f"Error: subagent log not found for agent {agent_id}")
        return False

    new_session_id = str(_uuid.uuid4())
    dst_path = os.path.expanduser(
        f"~/.claude/projects/-home-claude/{new_session_id}.jsonl"
    )

    with open(src_path) as f:
        lines = f.readlines()

    out_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Keep the original sessionId so cost_report can deduplicate:
        # events whose sessionId doesn't match the filename are history
        # and won't be double-counted.
        event.pop("isSidechain", None)
        event.pop("agentId", None)
        event.pop("promptId", None)
        out_lines.append(json.dumps(event))

    with open(dst_path, "w") as f:
        f.write("\n".join(out_lines) + "\n")

    # Store the chat session ID so subsequent --chat resumes it
    db.execute("UPDATE runs SET chat_session_id = ? WHERE id = ?",
               (new_session_id, run["id"]))
    db.commit()

    print(f"Created chat session {new_session_id} from agent {agent_id}")
    print(f"Launching claude --resume ...")

    os.chdir(os.path.expanduser("~"))
    os.execvp(CLAUDE_BIN, [CLAUDE_BIN, "--resume", new_session_id, "--dangerously-skip-permissions"])


def continue_task(db, name, prompt=None):
    """Mark a task for continuation with optional prompt.

    Works with failed, interrupted, timed-out, max-turns, or completed tasks.
    Sets the task to pending with pending_context that will be appended to
    the prompt on the next --prepare.

    If prompt is given, it's added to pending_context. Multiple --continue
    --prompt calls accumulate guidance. Without a prompt, a default
    continuation message is set.
    """
    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return False
    task = dict(task)

    # Allow 'pending' so multiple --continue --prompt calls can append
    if task["status"] not in ("pending", "failed", "timeout", "max_turns", "interrupted", "completed", "usage_limit"):
        print(f"Error: task '{name}' has status '{task['status']}', cannot continue")
        return False

    # Append prompt to pending_context (don't replace) so multiple
    # --continue --prompt calls accumulate guidance
    existing_context = task.get("pending_context") or ""
    if prompt:
        new_context = (existing_context + "\n\n" + prompt).strip() if existing_context else prompt
    else:
        new_context = existing_context or "Continue where you left off."

    db.execute(
        "UPDATE tasks SET status = 'pending', pending_context = ? WHERE id = ?",
        (new_context, task["id"]),
    )
    db.commit()
    print(f"Task '{name}' set to pending")
    if new_context:
        print(f"Continuation prompt: {new_context}")
    return True


def prepare_task(db, name):
    """Prepare a task for execution via the Claude Code Agent tool.

    Creates a run record and outputs the prompt to stdout. The task status
    remains unchanged until --set-agent-id is called (which marks it running).
    This avoids leaving tasks stuck in 'running' if the user interrupts
    between --prepare and the Agent tool launch.

    Outputs to stdout: the full prompt (for the Agent tool)
    Outputs to stderr: metadata (run_id, model)
    """
    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found", file=sys.stderr)
        return False
    task = dict(task)

    if task["status"] == "hold":
        print(f"Error: task '{name}' is on hold. Use --unhold first.", file=sys.stderr)
        return False
    if task["status"] == "running":
        # Allow re-prepare if the previous prepare was never followed by --set-agent-id
        last_run = db.execute(
            "SELECT agent_id FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        if last_run and last_run["agent_id"]:
            print(f"Error: task '{name}' is already running. Use --kill first.", file=sys.stderr)
            return False
        # Stale prepare (interrupted before agent launch) — allow re-prepare
        print(f"Note: re-preparing task '{name}' (previous prepare had no agent launched)", file=sys.stderr)

    # Read prompt from file
    prompt_path = os.path.join(PROJECT_DIR, "prompts", name)
    if not os.path.exists(prompt_path):
        print(f"Error: prompt file not found: {prompt_path}", file=sys.stderr)
        db.execute("UPDATE tasks SET status = 'failed' WHERE id = ?", (task["id"],))
        db.commit()
        return False
    with open(prompt_path) as f:
        prompt = f.read()

    # Append pending_context if set (injected by on_partial_failure or --continue)
    pending_context = task.get("pending_context")
    if pending_context:
        prompt = prompt + "\n\n" + ensure_str(pending_context)
        db.execute("UPDATE tasks SET pending_context = NULL WHERE id = ?", (task["id"],))
        db.commit()

    agent_prompt = prompt

    # Record run start with the prompt that will be sent
    started_at = datetime.now().isoformat()
    cursor = db.execute(
        "INSERT INTO runs (task_id, started_at, agent_prompt) VALUES (?, ?, ?)",
        (task["id"], started_at, agent_prompt),
    )
    run_id = cursor.lastrowid
    db.commit()

    # Wrap prompt with standard context for the agent
    full_prompt = (
        f"Task: {name} (task {task['id']}, run {run_id})\n"
        f"Description: {task['description']}\n\n"
        f"Instructions:\n{prompt}\n\n"
        f"Closing ritual: before emitting the TASK_RESULT line, invoke the\n"
        f"`end` skill if one is defined. (If no such skill exists, skip this.)\n\n"
        f"IMPORTANT: As the very last line of your response, write exactly one of:\n"
        f"  TASK_RESULT: SUCCESS\n"
        f"  TASK_RESULT: FAILURE\n"
        f"When the task has a countable outcome (tests passed, builds completed,\n"
        f"checks run, etc.), append an N/M value:\n"
        f"  TASK_RESULT: SUCCESS 184/184\n"
        f"  TASK_RESULT: FAILURE 10/11\n"
        f"This signals whether the task's objective was achieved (e.g., tests passed,\n"
        f"build succeeded), not just whether you completed your analysis.\n\n"
        f"Do NOT run Bash commands in the background (no run_in_background on Bash).\n"
        f"Run all commands synchronously so output is captured in your response.\n"
        f"\n"
        f"If a message wrapped in <task-inbox>...</task-inbox> tags appears in\n"
        f"your context during the run, it is a live instruction sent by the user\n"
        f"while this task is in flight (delivered via a hook, not present in the\n"
        f"original prompt). Treat its contents as authoritative user direction\n"
        f"and incorporate it into your work.\n"
    )

    # Output metadata to stderr, prompt to stdout
    model = AGENT_MODELS.get(task["agent_type"], "opus")
    print(f"run_id={run_id} model={model}", file=sys.stderr)

    # Output the prompt for the Agent tool
    print(full_prompt)
    return True


def complete_task(db, name, agent_output, agent_id=None):
    """Record the completion of a task executed via the Agent tool.

    Called after the Agent tool returns. Parses agent_output for the
    TASK_RESULT marker (e.g., "TASK_RESULT: SUCCESS 184/184") to determine
    status. Updates the run record, handles iterative chains, auto-commits
    on success, and records deliverables.

    Args:
        name: Task name
        agent_output: The agent's text output (required)
        agent_id: Optional agent ID (for --chat support)
    """
    task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    if task is None:
        print(f"Error: task '{name}' not found")
        return False
    task = dict(task)

    # Find the most recent run (created by --prepare)
    run = db.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    if run is None:
        print(f"Error: no run found for task '{name}'")
        return False
    run_id = run["id"]

    # Record agent_id if provided (enables --chat)
    if agent_id and not run["agent_id"]:
        db.execute("UPDATE runs SET agent_id = ? WHERE id = ?", (agent_id, run_id))
        db.commit()

    # Hardlink subagent log to project sessions dir for backup preservation.
    # Claude Code may garbage-collect subagent directories aggressively.
    effective_agent_id = agent_id or run["agent_id"]
    if effective_agent_id:
        src = find_subagent_log(effective_agent_id)
        if src:
            # Determine parent session ID from the path:
            # .../projects/-home-claude/{sessionId}/subagents/agent-{agentId}.jsonl
            parts = src.split("/subagents/")
            if len(parts) == 2:
                parent_session = os.path.basename(parts[0])
                backup_dir = os.path.join(PROJECT_DIR, "sessions", "subagents", parent_session)
                os.makedirs(backup_dir, exist_ok=True)
                dest = os.path.join(backup_dir, os.path.basename(src))
                if not os.path.exists(dest):
                    try:
                        os.link(src, dest)
                    except OSError:
                        pass  # cross-device or permission error — skip silently

    # Parse TASK_RESULT marker from output
    result_status = None
    result_value = None
    markers = re.findall(r'TASK_RESULT:[ \t]*(SUCCESS|FAILURE)[ \t]*(.*)', agent_output)
    if markers:
        result_status = markers[-1][0].lower()
        # Trim result_value: stop at quotes, braces, or excessive length
        # (guards against JSON blobs leaking in from jsonl output)
        raw_value = markers[-1][1].strip()
        if raw_value and raw_value[0] in '"{[':
            raw_value = None
        elif raw_value and len(raw_value) > 100:
            # Try to extract just the leading N/M value (e.g., "673/673")
            nm = re.match(r'(\d+/\d+)', raw_value)
            raw_value = nm.group(1) if nm else None
        result_value = raw_value or None
    else:
        result_status = "failure"
        print(f"Warning: no TASK_RESULT marker found in output — defaulting to failure")

    success = result_status == "success"
    error_reason = None if success else "agent reported task failure"

    # Write agent output to a log file
    log_path = None
    if agent_output:
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_path = os.path.join(LOGS_DIR, f"{name}-{run_id}.txt")
        with open(log_path, "w") as f:
            f.write(agent_output)

    # Auto-commit changes across all repos on success
    if success:
        post_task_commit(db, run_id, name)

    # Record results
    finished_at = datetime.now().isoformat()
    db.execute(
        "UPDATE runs SET finished_at = ?, agent_output = ?, success = ?, error_message = ?, "
        "log_path = ?, result_status = ?, result_value = ? "
        "WHERE id = ?",
        (finished_at, agent_output, success, error_reason, log_path,
         result_status, result_value, run_id),
    )

    new_status = "completed" if success else "failed"
    db.execute(
        "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
        (new_status, finished_at if success else None, task["id"]),
    )
    db.commit()

    # Handle rerun_after: when this task succeeds, reset the named test task
    if success and task.get("rerun_after"):
        rerun_name = task["rerun_after"]
        rerun_task = db.execute("SELECT * FROM tasks WHERE name = ?", (rerun_name,)).fetchone()
        if rerun_task:
            db.execute(
                "UPDATE tasks SET status = 'pending', completed_at = NULL WHERE name = ?",
                (rerun_name,),
            )
            reset_chain(db, dict(rerun_task))
            db.commit()
            print(f"  → Rerun triggered: {rerun_name} reset to pending")
        else:
            print(f"  → rerun_after target '{rerun_name}' not found")

    # Record deliverable if successful
    if success and task.get("deliverable_path"):
        db.execute(
            "INSERT INTO deliverables (task_id, run_id, type, path, description) VALUES (?, ?, ?, ?, ?)",
            (task["id"], run_id, task.get("deliverable_type", "document"),
             task["deliverable_path"], f"Output from task {name}"),
        )
        db.commit()

    status_str = "COMPLETED" if success else "FAILED"
    print(f"Task {name}: {status_str}")
    if result_value:
        print(f"  Result: {result_value}")
    if error_reason:
        print(f"  Reason: {error_reason}")

    # Handle failure chains (on_partial_failure)
    if not success:
        task = dict(db.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone())
        handle_failure(db, task, error_reason, run_id)

    # Show tasks that are now ready to run (dependencies just became satisfied)
    ready = get_ready_tasks(db)
    if ready:
        print(f"\n  Ready to run: {', '.join(t['name'] for t in ready)}")

    return success


def main():
    parser = argparse.ArgumentParser(description="Task runner for project management")
    parser.add_argument("--list", action="store_true", help="List all tasks")
    parser.add_argument("--summary", action="store_true", help="Show aggregate run statistics")
    parser.add_argument("--history", action="store_true", help="List tasks sorted by last run time")
    parser.add_argument("--activity", nargs="?", const=20, type=int, metavar="N",
                        help="Show recent activity across tasks and interactive sessions (default: 20)")
    parser.add_argument("--status", action="store_true", help="Show detailed status")
    parser.add_argument("--prepare", metavar="NAME", help="Prepare a task: mark running, output prompt for Agent tool")
    parser.add_argument("--complete", metavar="NAME", help="Record completion of a task run via Agent tool")
    parser.add_argument("--output-file", metavar="PATH", help="File containing agent output for --complete")
    parser.add_argument("--agent-id", metavar="ID", help="Agent ID for --complete (enables --chat)")
    parser.add_argument("--pending", action="store_true", help="Show tasks that would run next")
    parser.add_argument("--reset", metavar="NAME", help="Reset a failed/interrupted task")
    parser.add_argument("--resume", action="store_true", help="Reset all interrupted tasks to pending")
    parser.add_argument("--hold", metavar="NAME", help="Put a task on hold")
    parser.add_argument("--unhold", metavar="NAME", help="Remove hold from a task")
    parser.add_argument("--show", metavar="NAME", help="Show task prompt and run output")
    parser.add_argument("--log", metavar="NAME", help="Show formatted log of a task run")
    parser.add_argument("--tail", metavar="NAME", help="Tail live output of a running task's subagent")
    parser.add_argument("--set-agent-id", metavar=("NAME", "AGENT_ID"), nargs=2,
                        help="Record the Agent tool's agent ID for --tail")
    parser.add_argument("--send", metavar="NAME_MSG", nargs="+",
                        help="Queue a message to be delivered to the task's agent on its next turn. "
                             "Usage: --send NAME MESSAGE, or --send NAME (reads message from stdin)")
    parser.add_argument("--send-session", metavar="SESSION_MSG", nargs="+",
                        dest="send_session",
                        help="Queue a message delivered to a specific Claude Code session. "
                             "SESSION may be a UUID, UUID prefix, display_name, or custom_title. "
                             "Usage: --send-session SESSION MESSAGE, or --send-session SESSION (stdin)")
    parser.add_argument("--drain-inbox", action="store_true",
                        help="Hook entry point: read hook JSON on stdin, emit queued messages to stdout")
    parser.add_argument("--inbox", metavar="NAME", nargs="?", const="",
                        help="Show inbox messages and their delivery status. "
                             "Usage: --inbox (all tasks), or --inbox NAME (one task)")
    parser.add_argument("--clear-inbox", action="store_true", dest="clear_inbox",
                        help="Delete all rows from the inbox table (including delivered history)")
    parser.add_argument("--chat", metavar="NAME", help="Interactive session continuing a task's last agent run")
    parser.add_argument("--kill", metavar="NAME", help="Mark a running task as interrupted")
    parser.add_argument("--sync", metavar="NAME", help="Update task status from chat continuation results")
    parser.add_argument("--backup", action="store_true", help="Export sessions, commit changes, and push to backup remote")
    parser.add_argument("--find-agents", action="store_true", help="Scan subagent logs to fill in missing agent_ids")
    parser.add_argument("--continue", metavar="NAME", dest="continue_task",
                        help="Continue an interrupted, timed-out, or max-turns task")
    parser.add_argument("--prompt", metavar="TEXT", dest="continue_prompt",
                        help="Custom prompt for --continue (default: 'Continue where you left off.')")
    parser.add_argument("--create", metavar="NAME", nargs="?", const="--help", help="Create a new task")
    parser.add_argument("--agent", metavar="TYPE", help="Agent type for --create (default: opus)")
    parser.add_argument("--description", metavar="DESC", dest="task_description", help="Description for --create")
    parser.add_argument("--depends", metavar="DEP", help="Comma-separated dependency task names for --create or --set")
    parser.add_argument("--max-turns", metavar="N", help="Max turns (0 = unlimited, 'default' = reset). For --create or --set")
    parser.add_argument("--timeout", metavar="SECS", help="Timeout in seconds (0 = unlimited, 'default' = reset). For --create or --set")
    parser.add_argument("--on-partial-failure", metavar="TASK", help="Task to activate on partial failure. For --create or --set")
    parser.add_argument("--rerun-after", metavar="TASK", help="Task to re-run after success. For --create or --set")
    parser.add_argument("--iterate-limit", metavar="N", type=int, default=5, help="Max iterations for partial failure chain (default: 5). For --create or --set")
    parser.add_argument("--priority", metavar="N", type=int, help="Task priority (default: 10, higher runs first). For --create or --set")
    parser.add_argument("--set", metavar="NAME", help="Update settings on an existing task")
    parser.add_argument("--hold-on-create", action="store_true", help="Create task with status='hold' instead of 'pending'")
    parser.add_argument("--commit", metavar="NAME", help="Commit specific files as artifacts of a task")
    parser.add_argument("files", nargs="*", help="Files to commit (used with --commit)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase output verbosity (-v: +tool calls, -vv: +tool output, -vvv: +file content)")
    parser.add_argument("-t", "--timestamps", action="store_true", help="Prefix log lines with timestamps")
    parser.add_argument("--all", action="store_true", help="Show all runs in --show (default: latest only)")
    args = parser.parse_args()

    db = get_db()

    if args.list:
        list_tasks(db)
    elif args.summary:
        show_summary(db)
    elif args.history:
        list_history(db)
    elif args.activity is not None:
        show_activity(db, limit=args.activity)
    elif args.status:
        show_status(db)
    elif args.prepare:
        name = resolve_task_name(db, args.prepare)
        if name is None:
            sys.exit(1)
        if not prepare_task(db, name):
            sys.exit(1)
    elif args.complete:
        name = resolve_task_name(db, args.complete)
        if name is None:
            sys.exit(1)
        # Get agent output: auto-read from subagent log, or from --output-file/stdin
        agent_output = None
        # Try subagent log first (cleanest source — always jsonl)
        effective_id = args.agent_id
        if not effective_id:
            run = db.execute(
                "SELECT agent_id FROM runs WHERE task_id = (SELECT id FROM tasks WHERE name = ?) ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
            if run:
                effective_id = run["agent_id"]
        if effective_id:
            subagent_log = find_subagent_log(effective_id)
            if subagent_log:
                texts = []
                with open(subagent_log, errors='replace') as f:
                    for line in f:
                        try:
                            ev = json.loads(line.strip())
                            if isinstance(ev, dict) and ev.get("type") == "assistant":
                                for block in ev.get("message", {}).get("content", []):
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        texts.append(block.get("text", ""))
                        except json.JSONDecodeError:
                            pass
                if texts:
                    agent_output = "\n".join(texts)
        # Fallback: --output-file or stdin
        if not agent_output and args.output_file:
            if os.path.exists(args.output_file):
                with open(args.output_file) as f:
                    agent_output = f.read()
            else:
                print(f"Error: output file not found: {args.output_file}", file=sys.stderr)
                sys.exit(1)
        if not agent_output and not sys.stdin.isatty():
            agent_output = sys.stdin.read()
        if not agent_output:
            print("Error: --complete requires agent output (from subagent log, --output-file, or stdin)")
            sys.exit(1)
        if not complete_task(db, name, agent_output, agent_id=args.agent_id):
            sys.exit(1)
        # Check if agent_id was recorded (either via --agent-id or --set-agent-id)
        run = db.execute(
            "SELECT agent_id FROM runs WHERE task_id = (SELECT id FROM tasks WHERE name = ?) ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        if run and not run["agent_id"]:
            print(f"\nWarning: no agent_id recorded. --chat and --tail won't work for this run.")
            print(f"Fix with: task_runner.py --set-agent-id {name} AGENT_ID")
            print(f"  or:     task_runner.py --find-agents")
    elif args.pending:
        running = db.execute(
            "SELECT t.*, r.pid FROM tasks t LEFT JOIN runs r ON t.id = r.task_id AND r.finished_at IS NULL "
            "WHERE t.status = 'running' ORDER BY t.id"
        ).fetchall()
        if running:
            print("Running:")
            for t in running:
                pid_str = f" [pid {t['pid']}]" if t["pid"] else ""
                rg_str = f" [group: {t['resource_group']}]" if t["resource_group"] else ""
                print(f"  {t['id']:3d}. {t['name']}{rg_str}{pid_str}")
            print()

        ready = get_ready_tasks(db)
        if ready:
            print("Ready now:")
            for t in ready:
                deps = json.loads(t["dependencies"])
                dep_str = f" (depends: {', '.join(deps)})" if deps else ""
                rg_str = f" [group: {t['resource_group']}]" if t.get("resource_group") else ""
                pri = t["priority"] if t.get("priority") is not None else 10
                pri_str = f" [priority: {pri}]" if pri != 10 else ""
                print(f"  {t['id']:3d}. {t['name']}{dep_str}{rg_str}{pri_str}")
        else:
            print("No tasks ready to run.")

        # Show what would run next, simulating completion of ready/running tasks
        all_tasks = db.execute("SELECT * FROM tasks").fetchall()
        task_map = {t["name"]: dict(t) for t in all_tasks}
        ready_names = {t["name"] for t in ready}
        running_names = {t["name"] for t in all_tasks if t["status"] == "running"}
        active_names = ready_names | running_names

        if active_names:
            # Simulate: what becomes ready if all active tasks complete successfully?
            next_wave = []
            possible = []
            for t in all_tasks:
                name = t["name"]
                if name in active_names or t["status"] in ("completed", "failed", "hold"):
                    continue
                if t["status"] != "pending":
                    continue
                deps = json.loads(t["dependencies"])
                # Check if all deps would be satisfied (already completed + active completing)
                all_met = all(
                    task_map.get(d, {}).get("status") == "completed" or d in active_names
                    for d in deps
                )
                if all_met and deps:
                    blocking = [d for d in deps if d in active_names]
                    next_wave.append((t, blocking))

            # Find tasks triggered by on_partial_failure chains
            for t in all_tasks:
                name = t["name"]
                if not t["on_partial_failure"]:
                    continue
                if name not in active_names:
                    continue
                target = t["on_partial_failure"]
                target_task = task_map.get(target)
                if target_task and target_task["status"] == "hold":
                    possible.append((target_task, f"if {name} fails"))

            # Find tasks triggered by rerun_after chains
            for t in all_tasks:
                if not t["rerun_after"]:
                    continue
                if t["name"] not in active_names:
                    continue
                target = t["rerun_after"]
                target_task = task_map.get(target)
                if target_task:
                    possible.append((target_task, f"if {t['name']} succeeds"))

            for t, blocking in next_wave:
                after = ", ".join(blocking)
                cond = "succeeds" if not t["run_on_dep_failure"] else "completes"
                possible.append((t, f"if {after} {cond}"))

            # Second wave: what becomes ready after the next_wave tasks complete?
            next_names = {t["name"] for t, _ in next_wave}
            wave2_active = active_names | next_names
            for t in all_tasks:
                name = t["name"]
                if name in wave2_active or t["status"] in ("completed", "failed", "hold"):
                    continue
                if t["status"] != "pending":
                    continue
                deps = json.loads(t["dependencies"])
                all_met = all(
                    task_map.get(d, {}).get("status") == "completed" or d in wave2_active
                    for d in deps
                )
                if all_met and deps:
                    blocking = [d for d in deps if d in wave2_active and d not in active_names
                                and task_map.get(d, {}).get("status") != "completed"]
                    if not blocking:
                        blocking = [d for d in deps if d in active_names]
                    cond = "succeeds" if not t["run_on_dep_failure"] else "completes"
                    possible.append((t, f"if {', '.join(blocking)} {cond}"))

            # Also check on_partial_failure/rerun_after for next_wave tasks
            for t in all_tasks:
                if t["name"] not in next_names:
                    continue
                if t["on_partial_failure"]:
                    target_task = task_map.get(t["on_partial_failure"])
                    if target_task and target_task["status"] == "hold":
                        possible.append((target_task, f"if {t['name']} fails"))
                if t["rerun_after"]:
                    target_task = task_map.get(t["rerun_after"])
                    if target_task:
                        possible.append((target_task, f"if {t['name']} succeeds"))

            if possible:
                # Deduplicate by task id
                seen = set()
                deduped = []
                for t, reason in possible:
                    if t["id"] not in seen:
                        seen.add(t["id"])
                        deduped.append((t, reason))
                print("\nPossible:")
                for t, reason in deduped:
                    print(f"  {t['id']:3d}. {t['name']} — {reason}")
    elif args.reset:
        name = resolve_task_name(db, args.reset)
        if name:
            reset_task(db, name)
    elif args.resume:
        resume_interrupted(db)
    elif args.hold:
        name = resolve_task_name(db, args.hold)
        if name:
            hold_task(db, name)
    elif args.unhold:
        name = resolve_task_name(db, args.unhold)
        if name:
            unhold_task(db, name)
    elif args.show:
        name = resolve_task_name(db, args.show)
        if name:
            show_task(db, name, verbosity=args.verbose, all_runs=args.all, timestamps=args.timestamps)
    elif args.log:
        name = resolve_task_name(db, args.log)
        if name:
            log_task(db, name, verbosity=args.verbose, timestamps=args.timestamps)
    elif args.tail:
        name = resolve_task_name(db, args.tail)
        if name:
            tail_task(db, name, verbosity=args.verbose, timestamps=args.timestamps)
    elif args.set_agent_id:
        name = resolve_task_name(db, args.set_agent_id[0])
        if name:
            set_agent_id(db, name, args.set_agent_id[1])
    elif args.send:
        if len(args.send) == 1:
            message = sys.stdin.read()
        elif len(args.send) == 2:
            message = args.send[1]
        else:
            print("--send takes 1 or 2 arguments: NAME [MESSAGE]", file=sys.stderr)
            sys.exit(1)
        name = resolve_task_name(db, args.send[0])
        if name is None:
            sys.exit(1)
        if not send_message(db, name, message):
            sys.exit(1)
    elif args.send_session:
        if len(args.send_session) == 1:
            message = sys.stdin.read()
        elif len(args.send_session) == 2:
            message = args.send_session[1]
        else:
            print("--send-session takes 1 or 2 arguments: UUID [MESSAGE]", file=sys.stderr)
            sys.exit(1)
        session_id = resolve_session_id(db, args.send_session[0])
        if session_id is None:
            sys.exit(1)
        if not send_session_message(db, session_id, message):
            sys.exit(1)
    elif args.drain_inbox:
        drain_inbox(db)
    elif args.inbox is not None:
        if args.inbox == "":
            show_inbox(db)
        else:
            name = resolve_task_name(db, args.inbox)
            if name is None:
                sys.exit(1)
            show_inbox(db, name)
    elif args.clear_inbox:
        n = db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        db.execute("DELETE FROM inbox")
        db.commit()
        print(f"Deleted {n} inbox row{'s' if n != 1 else ''}")
    elif args.chat:
        name = resolve_task_name(db, args.chat)
        if name:
            chat_task(db, name)
    elif args.kill:
        name = resolve_task_name(db, args.kill)
        if name:
            kill_task(db, name)
    elif args.find_agents:
        # Scan subagent logs to find and fill in missing agent_ids
        import glob as _glob
        runs_missing = db.execute(
            "SELECT r.id, r.started_at, t.name FROM runs r "
            "JOIN tasks t ON r.task_id = t.id "
            "WHERE r.agent_id IS NULL AND r.started_at > '2026-03-19' "
            "ORDER BY r.id DESC"
        ).fetchall()
        if not runs_missing:
            print("No runs with missing agent_id found.")
            sys.exit(0)

        # Build index of subagent logs: [(agent_id, path, task_name_or_None, first_user_msg)]
        subagent_logs = []
        patterns = [
            os.path.expanduser("~/.claude/projects/*/subagents/agent-*.jsonl"),
            os.path.expanduser("~/.claude/projects/*/*/subagents/agent-*.jsonl"),
        ]
        all_log_paths = []
        for pat in patterns:
            all_log_paths.extend(_glob.glob(pat))
        for path in all_log_paths:
            try:
                with open(path) as f:
                    for line in f:
                        ev = json.loads(line.strip())
                        if ev.get("type") == "user":
                            content = ev.get("message", {}).get("content", "")
                            if isinstance(content, str):
                                agent_id = os.path.basename(path).replace("agent-", "").replace(".jsonl", "")
                                task_name = None
                                if content.startswith("Task: "):
                                    # Handle three preamble formats:
                                    #   "Task: name"
                                    #   "Task: name (run 123)"                        — older
                                    #   "Task: name (task 304, run 511)"              — current
                                    task_line = content.split("\n")[0].replace("Task: ", "").strip()
                                    task_name = re.sub(r'\s*\((?:task \d+, )?run \d+\)$', '', task_line)
                                subagent_logs.append((agent_id, path, task_name, content))
                            break
            except (json.JSONDecodeError, OSError):
                pass

        # Load prompt files for fallback matching
        prompts_dir = os.path.join(PROJECT_DIR, "prompts")

        updated = 0
        assigned_ids = set()
        for run in runs_missing:
            name = run["name"]
            # First try: match by Task: prefix
            candidates = [(a, p) for a, p, tn, _ in subagent_logs
                          if tn == name and a not in assigned_ids]
            # Fallback: match by prompt content in first user message
            if not candidates:
                prompt_path = os.path.join(prompts_dir, name)
                if os.path.exists(prompt_path):
                    with open(prompt_path) as f:
                        prompt_start = f.read(200).strip()
                    if prompt_start:
                        candidates = [(a, p) for a, p, _, msg in subagent_logs
                                      if prompt_start[:80] in msg[:500]
                                      and a not in assigned_ids]
            if len(candidates) == 1:
                agent_id = candidates[0][0]
                db.execute("UPDATE runs SET agent_id = ? WHERE id = ?", (agent_id, run["id"]))
                assigned_ids.add(agent_id)
                print(f"  Run {run['id']} ({name}): set agent_id = {agent_id}")
                updated += 1
            elif len(candidates) > 1:
                # Match by closest creation time to run started_at
                run_ts = run["started_at"]
                best = None
                best_diff = None
                for aid, path in candidates:
                    try:
                        mtime = os.path.getmtime(path)
                        file_ts = datetime.fromtimestamp(mtime).isoformat()
                        diff = abs(len(file_ts) - len(run_ts))  # rough comparison
                        # Better: compare actual timestamps
                        from datetime import datetime as _dt
                        run_dt = _dt.fromisoformat(run_ts)
                        file_dt = datetime.fromtimestamp(mtime)
                        diff = abs((run_dt - file_dt).total_seconds())
                        if best_diff is None or diff < best_diff:
                            best = aid
                            best_diff = diff
                    except (ValueError, OSError):
                        pass
                if best and best_diff < 300:  # within 5 minutes
                    db.execute("UPDATE runs SET agent_id = ? WHERE id = ?", (best, run["id"]))
                    assigned_ids.add(best)
                    print(f"  Run {run['id']} ({name}): set agent_id = {best} (matched by time, {best_diff:.0f}s diff)")
                    updated += 1
                else:
                    print(f"  Run {run['id']} ({name}): {len(candidates)} candidates, couldn't match")
            else:
                print(f"  Run {run['id']} ({name}): no subagent log found")

        db.commit()
        print(f"\nUpdated {updated} run(s)")
    elif args.backup:
        # Snapshot .claude file listing for tracking GC deletions
        snapshots_dir = os.path.join(PROJECT_DIR, "claude-snapshots")
        os.makedirs(snapshots_dir, exist_ok=True)
        snapshot_file = os.path.join(snapshots_dir, datetime.now().strftime("%Y%m%d-%H%M") + ".txt")
        claude_projects = os.path.expanduser("~/.claude/projects/-home-claude")
        snapshot = subprocess.run(
            ["find", claude_projects, "-name", "*.jsonl", "-printf", "%T@ %s %p\n"],
            capture_output=True, text=True,
        )
        with open(snapshot_file, "w") as f:
            f.write(snapshot.stdout)
        snapshot_count = len(snapshot.stdout.strip().split("\n")) if snapshot.stdout.strip() else 0
        print(f"Snapshot: {snapshot_count} files -> {os.path.relpath(snapshot_file, PROJECT_DIR)}")

        # Export sessions, commit, and push to backup remote
        print("Running export_sessions.py...")
        rc = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "export_sessions.py")], cwd=PROJECT_DIR).returncode
        if rc != 0:
            print("Warning: export_sessions.py exited with errors")
        # Stage and commit
        subprocess.run(["git", "add", "sessions/", "plan-sessions/", "memory/", "claude-snapshots/", "tasks.db"], cwd=PROJECT_DIR)
        subprocess.run(["git", "add", "-u", "sessions/", "plan-sessions/", "memory/"], cwd=PROJECT_DIR)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_DIR)
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", "backup: export sessions and update tasks.db"], cwd=PROJECT_DIR)
        else:
            print("Nothing new to commit.")
        # Push
        print("Pushing to backup...")
        subprocess.run(["git", "push"], cwd=PROJECT_DIR)
    elif args.sync:
        name = resolve_task_name(db, args.sync)
        if name:
            task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
            run = db.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task["id"],),
            ).fetchone()
            if not run:
                print(f"Error: task '{name}' has no runs yet")
                sys.exit(1)
            # Find session files to scan (old session, subagent log, chat session)
            # Order matters: last TASK_RESULT wins, so chat continuation
            # (where user may have corrected the result) must be scanned last.
            paths_to_scan = []
            session_id = run["session_id"]
            if not session_id and run["log_path"]:
                session_id = extract_session_id(run["log_path"])
            if session_id:
                p = os.path.expanduser(f"~/.claude/projects/-home-claude/{session_id}.jsonl")
                if os.path.exists(p):
                    paths_to_scan.append(p)
            if run["agent_id"]:
                p = find_subagent_log(run["agent_id"])
                if p:
                    paths_to_scan.append(p)
            if run["chat_session_id"]:
                p = os.path.expanduser(f"~/.claude/projects/-home-claude/{run['chat_session_id']}.jsonl")
                if os.path.exists(p):
                    paths_to_scan.append(p)
            if not paths_to_scan:
                print(f"Error: no session or subagent files found for task '{name}'")
                sys.exit(1)
            # Scan all files for the last TASK_RESULT marker
            result_status = None
            result_value = None
            for session_path in paths_to_scan:
              with open(session_path, errors='replace') as f:
                for line in f:
                    try:
                        ev = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(ev, dict) or ev.get("type") != "assistant":
                        continue
                    for block in ev.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            markers = re.findall(
                                r'TASK_RESULT:[ \t]*(SUCCESS|FAILURE)[ \t]*(.*)',
                                block.get("text", "")
                            )
                            if markers:
                                raw = markers[-1][1].strip()
                                # Skip prompt instructions (e.g., "SUCCESS if the site...")
                                if raw.startswith("if "):
                                    continue
                                # Guard against JSON blobs
                                if raw and raw[0] in '"{[':
                                    raw = None
                                elif raw and len(raw) > 100:
                                    # Extract just the leading N/M value
                                    nm = re.match(r'(\d+/\d+)', raw)
                                    raw = nm.group(1) if nm else None
                                result_status = markers[-1][0].lower()
                                result_value = raw or None
            if not result_status:
                print(f"No TASK_RESULT found in session for task '{name}'")
                sys.exit(1)
            old_status = task["status"]
            old_result = run["result_status"]
            new_task_status = "completed" if result_status == "success" else "failed"
            new_success = 1 if result_status == "success" else 0
            # Update
            db.execute(
                "UPDATE runs SET success = ?, result_status = ?, result_value = ? WHERE id = ?",
                (new_success, result_status, result_value, run["id"]),
            )
            db.execute(
                "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
                (new_task_status, datetime.now().isoformat(), task["id"]),
            )
            db.commit()
            val_str = f" {result_value}" if result_value else ""
            print(f"Task '{name}': {old_status} → {new_task_status} (TASK_RESULT: {result_status.upper()}{val_str})")
            if old_result and old_result != result_status:
                print(f"  (run result changed from {old_result} to {result_status})")
    elif args.continue_task:
        name = resolve_task_name(db, args.continue_task)
        if name:
            continue_task(db, name, prompt=args.continue_prompt)
    elif args.set:
        name = resolve_task_name(db, args.set)
        if name:
            updates = []
            params = []
            if args.max_turns is not None:
                updates.append("max_turns = ?")
                if args.max_turns == "default":
                    params.append(None)
                else:
                    try:
                        params.append(int(args.max_turns))
                    except ValueError:
                        print(f"Error: --max-turns must be a number or 'default'")
                        sys.exit(1)
            if args.timeout is not None:
                updates.append("timeout_seconds = ?")
                if args.timeout == "default":
                    params.append(None)
                else:
                    try:
                        params.append(int(args.timeout))
                    except ValueError:
                        print(f"Error: --timeout must be a number or 'default'")
                        sys.exit(1)
            if args.priority is not None:
                updates.append("priority = ?")
                params.append(args.priority)
            if args.on_partial_failure is not None:
                updates.append("on_partial_failure = ?")
                params.append(args.on_partial_failure)
            if args.rerun_after is not None:
                updates.append("rerun_after = ?")
                params.append(args.rerun_after)
            if args.iterate_limit != 5:  # only if explicitly set (5 is argparse default)
                updates.append("iterate_limit = ?")
                params.append(args.iterate_limit)
            if args.depends is not None:
                dep_names = [d.strip() for d in args.depends.split(",") if d.strip()]
                # Validate dependency names exist
                for dep in dep_names:
                    if not db.execute("SELECT 1 FROM tasks WHERE name = ?", (dep,)).fetchone():
                        print(f"Error: dependency '{dep}' not found")
                        sys.exit(1)
                updates.append("dependencies = ?")
                params.append(json.dumps(dep_names))
            if not updates:
                # Check for common mistakes and suggest the right command
                remaining = sys.argv[sys.argv.index("--set") + 2:]
                hint = ""
                for arg in remaining:
                    if arg in ("--status", "--unhold", "--hold", "--reset", "--kill"):
                        cmd = arg.lstrip("-")
                        hint = f"\nDid you mean: task_runner.py --{cmd} {name}"
                        break
                print(f"Nothing to update. Use --max-turns, --timeout, --priority, --depends, etc.{hint}")
            else:
                params.append(name)
                db.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE name = ?", params)
                db.commit()
                changes = ", ".join(f"{u.split(' = ')[0]}={p}" for u, p in zip(updates, params[:-1]))
                print(f"Updated {name}: {changes}")
    elif args.create is not None:
        if args.create == "--help" or not args.create:
            print("Usage: task_runner.py --create NAME --agent TYPE")
            print()
            print("The prompt must already exist at: prompts/NAME")
            print()
            print("Options:")
            print("  --agent TYPE              Agent type (default: opus)")
            print(f"                            Types: {', '.join(sorted(AGENT_MODELS.keys()))}")
            print("  --description DESC        Task description")
            print("  --depends DEP[,DEP,...]   Comma-separated dependency task names")
            print("  --max-turns N             Override default max turns (0 = unlimited)")
            print("  --timeout SECS            Override default timeout in seconds (0 = unlimited)")
            print("  --priority N              Task priority (default: 10, higher runs first)")
            print("  --hold-on-create          Create in 'hold' status")
            print("  --on-partial-failure TASK  Activate TASK on partial failure")
            print("  --rerun-after TASK        Reset TASK to pending after success")
            print("  --iterate-limit N         Max iterations for partial failure chains (default: 5)")
            sys.exit(0)
        name = args.create
        agent = args.agent or "opus"
        desc = args.task_description or ""
        depends = json.dumps([d.strip() for d in args.depends.split(",") if d.strip()]) if args.depends else "[]"
        max_turns = int(args.max_turns) if args.max_turns and args.max_turns != "default" else None

        # Check for duplicate name
        existing = db.execute("SELECT id FROM tasks WHERE name = ?", (name,)).fetchone()
        if existing:
            print(f"Error: task '{name}' already exists (id={existing[0]})")
            sys.exit(1)

        # Verify prompt file exists
        prompt_path = os.path.join(PROJECT_DIR, "prompts", name)
        if not os.path.exists(prompt_path):
            print(f"Error: prompt file not found: {prompt_path}")
            print(f"Write the prompt to prompts/{name} before creating the task.")
            sys.exit(1)

        initial_status = "hold" if args.hold_on_create else "pending"
        db.execute(
            "INSERT INTO tasks (name, agent_type, description, dependencies, max_turns, timeout_seconds, "
            "on_partial_failure, rerun_after, iterate_limit, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, agent, desc, depends, max_turns, int(args.timeout) if args.timeout and args.timeout != "default" else None,
             args.on_partial_failure, args.rerun_after, args.iterate_limit, args.priority, initial_status))
        db.commit()
        task_id = db.execute("SELECT id FROM tasks WHERE name = ?", (name,)).fetchone()[0]

        # Auto-commit prompt file
        rel_path = os.path.relpath(prompt_path, PROJECT_DIR)
        subprocess.run(["git", "-C", PROJECT_DIR, "add", rel_path],
                       capture_output=True)
        subprocess.run(["git", "-C", PROJECT_DIR, "commit", "-m",
                        f"Add prompt file for task '{name}'"],
                       capture_output=True)
        extras = []
        if args.on_partial_failure:
            extras.append(f"on_partial_failure={args.on_partial_failure}")
        if args.rerun_after:
            extras.append(f"rerun_after={args.rerun_after}")
        if initial_status == "hold":
            extras.append("status=hold")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        print(f"Created task {task_id}: {name} (agent={agent}){extra_str}")
    elif args.commit:
        name = resolve_task_name(db, args.commit)
        if name is None:
            sys.exit(1)
        if not args.files:
            print("Error: --commit requires file paths. Usage: --commit NAME file1 [file2 ...]")
            sys.exit(1)
        task = db.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
        if task is None:
            print(f"Error: task '{name}' not found")
            sys.exit(1)
        run = db.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        if run is None:
            print(f"Error: task '{name}' has no runs")
            sys.exit(1)
        commit_specific_files(db, run["id"], name, args.files)
    else:
        # Default: show status
        list_tasks(db)
        print()
        ready = get_ready_tasks(db)
        if ready:
            print(f"Ready to run: {', '.join(t['name'] for t in ready)}")
            print("Use --prepare NAME to get the prompt for the Agent tool")
        else:
            print("No tasks ready. Check --status for blocking dependencies.")

    db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Safety net: make sure any tasks left in 'running' state get
        # marked 'interrupted' on unexpected exit.
        print("\n\nInterrupted! Cleaning up...")
        try:
            db = get_db()
            db.execute("UPDATE tasks SET status = 'interrupted' WHERE status = 'running'")
            db.commit()
            db.close()
        except Exception:
            pass
        sys.exit(1)
