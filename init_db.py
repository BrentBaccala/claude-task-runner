#!/usr/bin/env python3
"""Initialize the tasks.db database with schema."""

import sqlite3
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from project_dir import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    dependencies TEXT DEFAULT '[]',
    status TEXT DEFAULT 'pending',
    deliverable_type TEXT,
    deliverable_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    resource_group TEXT,
    max_turns INTEGER,
    timeout_seconds INTEGER,
    run_on_dep_failure BOOLEAN DEFAULT 0,
    on_partial_failure TEXT,
    rerun_after TEXT,
    iterate_limit INTEGER DEFAULT 5,
    iterate_count INTEGER DEFAULT 0,
    pending_context TEXT,
    resume_session_id TEXT,
    priority INTEGER DEFAULT 10
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id),
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    log_path TEXT,
    agent_prompt TEXT,
    agent_output TEXT,
    tokens_used INTEGER,
    success BOOLEAN,
    error_message TEXT,
    committed_files TEXT,
    commit_state TEXT,
    cost_usd REAL,
    num_turns INTEGER,
    duration_ms INTEGER,
    pid INTEGER,
    result_status TEXT,
    result_value TEXT,
    session_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS deliverables (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id),
    run_id INTEGER REFERENCES runs(id),
    type TEXT,
    path TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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


def init_db():
    """Create the database with schema (no initial tasks)."""
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()

    # Print summary
    count = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    if count:
        cursor = db.execute("SELECT id, name, status, dependencies FROM tasks ORDER BY id")
        print("Existing tasks:")
        for row in cursor:
            deps = json.loads(row[3])
            dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
            print(f"  {row[0]:2d}. [{row[2]}] {row[1]}{dep_str}")
        print(f"\nTotal: {count} tasks")
    else:
        print("Database initialized (no tasks).")

    db.close()


if __name__ == "__main__":
    init_db()
