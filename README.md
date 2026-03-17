# Claude Task Runner

A task orchestration system for [Claude Code](https://claude.ai/claude-code) agents.

Manages a database of tasks with dependencies, launches Claude Code agents
as subprocesses, captures structured logs, tracks costs and results, and
supports iterative debug/fix loops.

## Features

- **Task management** with dependencies, priorities, and resource groups
- **Multiple agent types** (opus, sonnet) with configurable timeouts and turn limits
- **Iterative chains** — automatic test/fix/retest loops with progress detection
- **Structured logging** — stream-json logs with tool calls, costs, and timing
- **Auto-commit** — commits agent changes across git repos after successful tasks
- **Session continuations** — resume interrupted tasks from where they left off
- **Interactive chat** — drop into an interactive session continuing a task's work
- **Live monitoring** — tail running task output in real-time
- **Backup** — export sessions and push to a backup remote in one command

## Quick Start

```bash
# Initialize the database
python3 init_db.py

# Write a task prompt
cat > prompts/my-task << 'EOF'
Build the project and run the test suite.
Report results as TASK_RESULT: SUCCESS N/M or TASK_RESULT: FAILURE N/M.
EOF

# Create and run the task
python3 task_runner.py --create my-task --agent coder
python3 task_runner.py --run my-task

# Monitor and view results
python3 task_runner.py --tail my-task    # live output
python3 task_runner.py --show my-task    # formatted results
python3 task_runner.py --show my-task -v # with tool calls
```

## Files

| File | Purpose |
|------|---------|
| `task_runner.py` | Main script — runs agents, manages tasks, auto-commits |
| `format_session.py` | Format/browse Claude Code session logs |
| `export_sessions.py` | Export session files and memory into the project directory |
| `init_db.py` | Database schema (creates `tasks.db`) |
| `agent-settings.json` | PreToolUse hooks passed to agents |

## Task Lifecycle

```
pending → running → completed
                  → failed
                  → interrupted (Ctrl+C or --kill)
                  → timeout
                  → max_turns
                  → usage_limit

hold ↔ pending (via --hold / --unhold)
failed/interrupted/timeout/max_turns/completed → pending (via --continue)
any non-running → pending (via --reset)
```

## Commands

```bash
# Viewing
task_runner.py --list                   # All tasks with status
task_runner.py --history                # Tasks by last run time, with cost
task_runner.py --summary                # Aggregate stats by agent type
task_runner.py --pending                # Tasks that would run on --run-ready
task_runner.py --show NAME              # Task output (all run headers, last run detail)
task_runner.py --show NAME -v           # Include tool calls
task_runner.py --show NAME -vv          # Include tool output
task_runner.py --show NAME --all        # Full detail for every run

# Running
task_runner.py --run NAME               # Run a specific task
task_runner.py --run-ready              # Run all ready tasks
task_runner.py --continue NAME          # Resume from last session
task_runner.py --continue NAME --prompt "Try X"
task_runner.py --chat NAME              # Interactive session on a task
task_runner.py --tail NAME              # Tail live output

# Managing
task_runner.py --reset NAME             # Reset to pending
task_runner.py --hold NAME / --unhold NAME
task_runner.py --kill NAME              # Kill running task
task_runner.py --sync NAME              # Update status from chat continuation
task_runner.py --backup                 # Export sessions, commit, push

# Creating
task_runner.py --create NAME --agent TYPE
task_runner.py --create NAME --agent TYPE --depends dep1,dep2
task_runner.py --create NAME --agent TYPE --hold-on-create
task_runner.py --create NAME --agent TYPE --priority 20
```

## Iterative Chains

Set up automatic test/fix loops:

```bash
# Test task: activates fix task on partial failure
task_runner.py --create my-test --agent tester \
  --on-partial-failure my-fix --iterate-limit 5

# Fix task: re-runs test after success
task_runner.py --create my-fix --agent coder \
  --rerun-after my-test --hold-on-create
```

The loop continues as long as the test result improves (e.g., `FAILURE 10/11` → `FAILURE 11/11`).

## TASK_RESULT Protocol

Agents must write a result marker as the last line of their response:

```
TASK_RESULT: SUCCESS
TASK_RESULT: FAILURE
TASK_RESULT: SUCCESS 184/184
TASK_RESULT: FAILURE 10/11
```

The task runner parses this to determine success/failure and track progress.

## Agent Types

| Type | Model | Use For |
|------|-------|---------|
| `coder` | opus | Code changes, bug fixes |
| `builder` | opus | Building from source |
| `tester` | opus | Running test suites |
| `researcher` | opus | Analysis, literature search |
| `explorer` | opus | Codebase exploration |
| `documenter` | opus | Documentation |
| `sonnet` | sonnet | Cheaper/faster tasks |

## Requirements

- [Claude Code](https://claude.ai/claude-code) CLI installed (`~/.local/bin/claude`)
- Python 3.10+
- SQLite3

## License

MIT
