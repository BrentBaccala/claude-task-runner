"""Find the project directory containing tasks.db.

Search order:
1. TASK_RUNNER_PROJECT env variable
2. Current working directory (if it has tasks.db)
3. ~/*/tasks.db (first match, error if ambiguous)
4. Current working directory (fallback for init)
"""

import glob
import os
import sys


def _is_valid_db(path):
    """Check that a tasks.db file is non-empty (not a stray from a bad init)."""
    return os.path.exists(path) and os.path.getsize(path) > 0


def find_project_dir():
    env = os.environ.get("TASK_RUNNER_PROJECT")
    if env:
        return os.path.expanduser(env)
    cwd = os.getcwd()
    if _is_valid_db(os.path.join(cwd, "tasks.db")):
        return cwd
    matches = [m for m in glob.glob(os.path.expanduser("~/*/tasks.db")) if _is_valid_db(m)]
    if len(matches) == 1:
        return os.path.dirname(matches[0])
    if len(matches) > 1:
        print(f"Warning: multiple tasks.db found: {', '.join(os.path.dirname(m) for m in matches)}", file=sys.stderr)
        print(f"Set TASK_RUNNER_PROJECT or run from the project directory.", file=sys.stderr)
    return cwd


PROJECT_DIR = find_project_dir()
DB_PATH = os.path.join(PROJECT_DIR, 'tasks.db')


def cwd_to_bucket(cwd):
    """Encode an absolute cwd as the Claude Code project-bucket dirname.

    Claude Code creates one subdirectory under ~/.claude/projects/ per
    cwd a session was started from. The dirname is the absolute cwd
    with each "/" replaced by "-" — so cwd "/home/alice" maps to bucket
    "-home-alice". The encoding is lossy (a real "-" in the cwd is
    indistinguishable from a "/" separator on decode), so this is one-way
    only; recover the original cwd from a session's events instead."""
    return cwd.replace("/", "-")


CLAUDE_PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
HOME_BUCKET = cwd_to_bucket(os.path.expanduser("~"))
HOME_BUCKET_DIR = os.path.join(CLAUDE_PROJECTS_ROOT, HOME_BUCKET)
