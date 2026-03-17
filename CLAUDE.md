# CLAUDE.md — Claude Task Runner

## Overview

This is a task orchestration system for Claude Code agents. It manages tasks
in an SQLite database, launches Claude Code as subprocesses via `claude --print`,
captures structured stream-json logs, and tracks costs, results, and session IDs
for continuations.

## Key Files

| File | Purpose |
|------|---------|
| `task_runner.py` | Main script — CLI, task execution, log parsing, auto-commit |
| `format_session.py` | Format Claude Code `.jsonl` session logs for terminal display |
| `export_sessions.py` | Hardlink session/memory files from `~/.claude` into the project |
| `init_db.py` | Database schema definition (creates `tasks.db`) |
| `agent-settings.json` | PreToolUse hooks and SessionStart env injection for agents |

## Architecture

### Task Execution Flow

1. `--run` reads the prompt from `prompts/NAME`
2. Wraps it with TASK_RESULT instructions and task metadata
3. Launches `claude --print --output-format stream-json`
4. Pipes prompt via stdin, captures stdout to a log file
5. Injects wall-clock timestamps into every log event
6. Parses the log for TASK_RESULT markers, cost, turns, duration
7. Updates the database and auto-commits changes

### Stream-JSON Log Format

Every tool call, assistant message, and result is captured. The task runner
injects a `timestamp` field into each event for timing analysis. The
`format_log()` function renders these into readable text for `--show`.

Sub-sessions: Claude Code 2.1.69+ emits extra init/result cycles when
background agents complete. `format_log()` suppresses sub-session result
events. `extract_result_stats()` uses the first result's turns/duration
(main session) but the last result's cost (cumulative).

### Session Management

- Each run's Claude session ID is stored in the `runs` table
- `--continue` uses `claude --resume SESSION_ID` to pick up where it left off
- `--chat` execs an interactive `claude --resume` for the last run's session
- `--sync` scans the full session `.jsonl` for TASK_RESULT updates from chat continuations
- `--show` renders chat continuations (events after the log's last timestamp)
  using `format_session.py`'s `process_events`

### Auto-Commit

After successful tasks, `post_task_commit()` scans all git repos under `~/`
for uncommitted changes and commits them. Infrastructure files and build
directories are excluded.

### Hooks (agent-settings.json)

- **SessionStart**: Exports `TASK_LIVE_LOG` and bash timeout env vars
- **PreToolUse (Bash)**: Blocks `run_in_background` to prevent orphaned processes
- **PreToolUse (Agent)**: Reminds model not to write TASK_RESULT before background agents complete

## Common Modifications

### Adding Agent Types

Add entries to `AGENT_MODELS`, `AGENT_TIMEOUTS`, and `AGENT_MAX_TURNS` dicts
near the top of `task_runner.py`.

### Changing the Prompt Wrapper

Edit the `full_prompt` construction in `run_task()` (~line 1793). The wrapper
provides TASK_RESULT instructions and tee/logging guidance.

### Database Schema Changes

Add migrations to `init_db.py`'s `MIGRATIONS` list. These run automatically
on startup. Always use `ALTER TABLE ... ADD COLUMN` with a try/except for
idempotency.

## Testing

There's no formal test suite. The `test-runner-smoke` task in the task database
exercises basic functionality. For manual testing:

```bash
python3 init_db.py                          # Fresh database
echo "Say hello. TASK_RESULT: SUCCESS" > prompts/test
python3 task_runner.py --create test --agent sonnet
python3 task_runner.py --run test
python3 task_runner.py --show test
```
