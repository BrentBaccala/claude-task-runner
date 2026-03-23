# CLAUDE.md — Claude Task Runner

## What This Is

The task runner (`task_runner.py`) manages tasks for Claude Code agents. It
tracks tasks in a SQLite database with dependencies, iterative chains,
auto-commit, and result recording. Tasks are executed via the Claude Code
**Agent tool** from within an interactive session — no `claude --print`
subprocesses.

## How It Works

The task runner is a management/tracking CLI. Execution happens through the
Claude Code Agent tool:

```
1. task_runner.py --prepare NAME           # Mark running, output prompt
2. Use the Agent tool with the prompt      # Claude Code spawns a subagent
3. task_runner.py --set-agent-id NAME ID   # Record agent ID (enables --tail)
4. task_runner.py --complete NAME \        # Record result, handle chains
     --output-file /tmp/result.txt        # (parses TASK_RESULT from output)
```

Step 3 is optional but enables `--tail` for live monitoring. The agent ID
comes from the Agent tool's result (the `agentId:` line). For background
agents, record it immediately after launch so `--tail` works while the
agent is still running.

The orchestrating Claude Code session (you, in an interactive chat) handles
the Agent tool invocation. The task runner handles everything else: prompt
assembly, run tracking, auto-commit, iterative chains, and dependency
resolution.

## Startup

On first use, initialize the database and create symlinks:

```bash
python3 init_db.py
ln -s ~/project/task_runner.py ~/.local/bin/task_runner.py
ln -s ~/project/format_session.py ~/.local/bin/format_session.py
```

This creates `tasks.db` with the schema and migrations. The database, prompts,
logs, and docs directories are project-specific data — they're gitignored in
this repo and belong in your project. The symlinks make the scripts available
on the PATH from any directory.

## Quick Reference

```bash
# Viewing
task_runner.py --list                   # All tasks with status and dependencies
task_runner.py --history                # Tasks sorted by last run time, with total cost
task_runner.py --summary                # Aggregate stats: runs, time, cost by agent type
task_runner.py --status                 # Detailed dependency resolution view
task_runner.py --pending                # Show tasks that would be prepared next
task_runner.py --show NAME              # Assistant text only
task_runner.py --show NAME --all        # All runs (default: latest only)
task_runner.py --show NAME -v           # + tool call summaries
task_runner.py --show NAME -vv          # + tool output
task_runner.py --show NAME -vvv         # + full file content (Write/Edit bodies)

# Executing (via Agent tool)
task_runner.py --prepare NAME           # Mark running, output prompt for Agent tool
task_runner.py --complete NAME \        # Record completion after Agent returns
  --output-file /tmp/result.txt        # (or pipe output via stdin)
task_runner.py --set-agent-id NAME ID   # Record agent ID (for --tail)
task_runner.py --tail NAME              # Tail live output of running agent
task_runner.py --tail NAME -v           # Include tool invocations
task_runner.py --tail NAME -vv          # Include tool output
task_runner.py --chat NAME              # Interactive session continuing last agent run
task_runner.py --continue NAME          # Set up a task for continuation
task_runner.py --continue NAME --prompt "Focus on X"  # With guidance

# Managing
task_runner.py --reset NAME             # Reset any non-pending task back to pending
task_runner.py --resume                 # Reset all interrupted tasks to pending
task_runner.py --hold NAME              # Pause a pending task
task_runner.py --unhold NAME            # Resume a held task
task_runner.py --kill NAME              # Mark a running task as interrupted
task_runner.py --sync NAME              # Update task status from session results
task_runner.py --backup                 # Export sessions, commit, push to backup remote

# Creating (write prompt to prompts/NAME first)
task_runner.py --create NAME --agent TYPE
task_runner.py --create NAME --agent TYPE --depends dep1,dep2
task_runner.py --create NAME --agent TYPE --priority 20   # higher runs first (default: 10)

# Updating task settings (--set ONLY accepts these options)
task_runner.py --set NAME --max-turns 0            # unlimited turns
task_runner.py --set NAME --max-turns default      # reset to agent-type default
task_runner.py --set NAME --timeout 0              # unlimited timeout
task_runner.py --set NAME --timeout default        # reset to agent-type default
task_runner.py --set NAME --priority 20            # higher runs first
task_runner.py --set NAME --depends dep1,dep2      # set dependencies
# NOTE: --set does NOT change status. Use --reset, --hold, --unhold, --kill instead.

# Committing
task_runner.py --commit NAME file1 file2   # Record files as task artifacts
```

**Names vs IDs**: Most commands accept either a task name or numeric ID.
Partial name matches work if unambiguous.

**Important**: Do NOT prepare/execute tasks unless explicitly asked.
Create the task and write its prompt, then let the user decide when to run it.
Do NOT use `--hold-on-create` unless the user specifically asks for it — create
tasks as pending (the default) so they're ready to run.

When the user says "continue task N" with new information, use
`--continue NAME --prompt "..."` to queue the continuation prompt. The task
won't execute until the user explicitly asks — that's the intended workflow.

## Executing a Task (Step-by-Step)

When the user asks you to run a task, follow these steps exactly:

### Step 1: Prepare
```bash
task_runner.py --prepare NAME
```
This marks the task as running, creates a run record, and outputs the
prompt to stdout. Capture the prompt text. Note the `model=` on stderr.

### Step 2: Launch the Agent
Use the Agent tool with:
- `prompt`: the full text output from --prepare
- `model`: the model from stderr (opus, sonnet, or haiku)
- `run_in_background`: **true** (always — tasks run in background by default)
- `description`: a short summary of the task

### Step 3: Record the Agent ID
Immediately after the Agent tool launches, extract the `agentId:` from
the result and record it:
```bash
task_runner.py --set-agent-id NAME AGENT_ID
```
This is **required** — it enables both `--tail` and `--chat`.
Do this immediately after launch (before the agent finishes) so
`--tail` works while it's running.

### Step 4: Complete
After the agent finishes, call `--complete`:
```bash
task_runner.py --complete NAME
```
`--complete` auto-reads the agent's output from its subagent log
(using the agent ID recorded in step 3). No need to pipe output.

`--complete` parses the output for `TASK_RESULT: SUCCESS/FAILURE`,
updates the database, auto-commits on success, and handles iterative
chains (on_partial_failure, rerun_after).

### Optional: Tail a Background Agent
If the agent was launched with `run_in_background: true`:
```bash
task_runner.py --tail NAME -v
```
This shows the agent's live output. Requires step 3 to have been done.
Run this from the Bash tool (it blocks until Ctrl+C or timeout).

### Step 5: Check for Ready Tasks
After `--complete` finishes, it prints any tasks that are now ready to
run (dependencies just became satisfied). **If there are ready tasks,
immediately start them** by going back to step 1. This is how sequential
task chains execute — you are the loop that the old `--run-ready` used
to provide.

If you launched a background agent and are waiting for it to finish,
you will receive a `<task-notification>` when it completes. When that
arrives, run step 4 (--complete) and then check for ready tasks.

## Creating Tasks

1. Write the prompt to `prompts/NAME` (no extension)
2. Run `task_runner.py --create NAME --agent TYPE`

The prompt file is the source of truth — it's read from disk each time the
task runs. The `runs` table records what was actually sent (`agent_prompt`
column) for each run, so you have a history even if the prompt file changes.

`--create` verifies the prompt file exists and auto-commits it to git.

```bash
# Basic
task_runner.py --create my-task --agent tester \
  --description "Run the test suite"

# With dependencies (won't run until deps complete)
task_runner.py --create run-tests --agent tester \
  --depends build-project
```

For iterative test/fix chains, create two tasks:

```bash
# Test task: on failure, activates the fix task
task_runner.py --create my-test --agent tester \
  --on-partial-failure my-fix --iterate-limit 5

# Fix task: on success, re-runs the test task
task_runner.py --create my-fix --agent coder \
  --rerun-after my-test --hold-on-create
```

The test task reports `TASK_RESULT: FAILURE N/M`. The task runner unholds
the fix task with context about what failed. When the fix succeeds, it
resets the test task to pending. The loop continues as long as N improves.

## Agent Types

The default agent types are `opus` and `sonnet`. Any string can be used as
an agent type — unknown types default to the opus model with no timeout or
turn limit.

| Type | Model | Use For |
|------|-------|---------|
| `opus` | opus | Complex tasks (default) |
| `sonnet` | sonnet | Straightforward tasks (cheaper/faster) |

To add custom types with different model mappings, edit the `AGENT_MODELS`
dict near the top of `task_runner.py`. The agent type maps to the `model`
parameter of the Claude Code Agent tool (`opus`, `sonnet`, `haiku`).

## Task Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Ready to run (or waiting for dependencies) |
| `hold` | Paused — won't run until explicitly unheld or activated |
| `running` | Agent is currently executing |
| `completed` | Finished successfully |
| `failed` | Agent finished but task failed |
| `timeout` | Agent was killed after exceeding time limit |
| `max_turns` | Agent hit the maximum number of turns |
| `usage_limit` | Agent hit its API usage/rate limit |
| `interrupted` | User killed the task with Ctrl+C or `--kill` |

Tasks with status `failed`, `interrupted`, `timeout`, `max_turns`, `usage_limit`,
or `completed` can be continued with `--continue`.

## Writing Task Prompts

### The TASK_RESULT Marker

Every prompt should instruct the agent to write a result marker as the **very
last line** of its response. The task runner parses this to determine success
or failure.

```
TASK_RESULT: SUCCESS
TASK_RESULT: FAILURE
```

Append an **N/M result value** whenever the task has a countable outcome
(tests passed, files processed, checks completed, etc.):

```
TASK_RESULT: SUCCESS 184/184
TASK_RESULT: FAILURE 10/11
TASK_RESULT: SUCCESS 3/3 targets built
```

The value is stored in the `runs` table (`result_value` column) and displayed
in `--history`. For iterative task chains, the N/M format is also used to
detect progress.

**Important**: The task runner wraps your prompt with standard context including
the TASK_RESULT instruction. But for clarity, include it in your prompt too.

### Long-Running Commands

For commands that take a while (builds, test suites), pipe output through tee
so it can be monitored in real-time via `--tail`:

```bash
make -j4 2>&1 | tee -a $TASK_LIVE_LOG
./run-tests.sh 2>&1 | tee -a $TASK_LIVE_LOG
```

Use `-a` (append) so multiple commands don't overwrite each other.

### Prompt Tips

- Be specific about paths, build directories, and environment setup
- Reference `CLAUDE.md` files in relevant repos for build/test instructions
- For test tasks, list the exact commands to run
- For debug tasks, point the agent at the failing test output:
  `python3 task_runner.py --show failing-task`

## Continuing Tasks (`--continue`)

When a task hits `failed`, `interrupted`, `timeout`, `max_turns`, `usage_limit`,
or even `completed`, use `--continue` to set it up for another run:

```bash
task_runner.py --continue my-task                              # marks pending
task_runner.py --continue my-task --prompt "Try a different approach"  # with guidance
task_runner.py --prepare my-task                               # then prepare for Agent tool
```

`--continue` sets `pending_context` on the task, which gets appended to
the prompt on the next `--prepare`. Multiple `--continue --prompt` calls
accumulate guidance. The default continuation message is "Continue where
you left off."

Note: unlike the old `claude --resume` approach, the agent starts a fresh
session. Include enough context in the prompt for the agent to understand
what happened previously. Use `--show NAME` to review prior run output.

## Iterative Task Chains

For tasks that benefit from a debug-fix-retest loop (e.g., test suites),
you can set up iterative chains that run automatically as long as progress
is being made.

### Concept

The simplest chain is two tasks — a test and a fix:

```
test task  ──(on_partial_failure)──→  fix task
    ↑                                    │
    └──────── (rerun_after) ────────────┘
```

1. Test task runs, reports `TASK_RESULT: FAILURE 10/11`
2. Task runner activates the fix task (unholds it, injects failure context)
3. Fix task runs, makes fixes, reports SUCCESS
4. Fix task has `rerun_after` → test task is reset to pending
5. Test task runs again. If result improves (e.g., 11/11), done. If no
   progress (still 10/11), loop stops.

If a rebuild step is needed between fix and retest, add a third task with
`--depends my-fix --rerun-after my-test`.

### Key Fields

| Field | On `--create` | Purpose |
|-------|---------------|---------|
| `on_partial_failure` | `--on-partial-failure TASK` | Task to activate when this task reports `TASK_RESULT: FAILURE` with a result value |
| `rerun_after` | `--rerun-after TASK` | Task to reset to pending after this task succeeds |
| `iterate_limit` | `--iterate-limit N` | Maximum iterations before stopping the loop (default: 5) |

### How Progress Is Detected

The task runner compares `result_value` between consecutive runs:

- **N/M format** (e.g., `10/11`): compares the numerator — `11/11 > 10/11`
- **Numeric** (e.g., `47`): compares as numbers — `48 > 47`
- **Different strings**: conservatively assumes progress
- **Identical values**: no progress — loop stops

### What `pending_context` Does

When `on_partial_failure` activates a fix task, it injects context about the
failure into the fix task's `pending_context` field. This context is prepended
to the fix task's prompt at run time and then cleared. It includes:

- Which test task failed and what the result was
- The previous result (for comparison)
- How to view the test output (`--show` command)

This means the fix task's base prompt stays generic ("analyze failures and fix
them"), while the injected context tells it what specifically failed this time.

## Dependencies

Tasks can depend on other tasks:

```bash
task_runner.py --create run-tests --agent tester --depends build-it
```

- A task won't run until all its dependencies have status `completed`
- Use `--pending` to see which tasks are ready for `--prepare`
- The `run_on_dep_failure` DB field (set via SQL, not `--create`) allows a
  task to run even if dependencies failed

### Resource Groups

Tasks in the same `resource_group` won't run concurrently. Use this for
tasks that need exclusive access to a build directory:

```sql
UPDATE tasks SET resource_group = 'singular-build' WHERE name IN ('build-lset', 'build-spielwiese');
```

## Failure Handling

Failed tasks stay in their failed state. Use `--reset` + `--prepare` to retry,
or `--continue` for tasks that were `failed`, `interrupted`, `timeout`, or
`max_turns`.

The one automatic behavior is `on_partial_failure` chains (see Iterative
Task Chains above): when a task reports `TASK_RESULT: FAILURE` with a
result value and has `on_partial_failure` set, the fix task is activated
if progress is being made.

## Auto-Commit

After a successful task, the task runner commits changes across all git repos
under `~/`. Excluded from auto-commit:

- `task_runner.py`, `init_db.py`, `tasks.db`, `agent-settings.json` (infrastructure)
- `build/` and `build-*` directories (build artifacts)

The committed files and commit SHAs are recorded in the run for traceability.

Tasks should also commit their own changes before exiting (unless the prompt
says otherwise). The task runner's auto-commit uses a generic message — the
agent's own commit with a descriptive message is preferred.

## Viewing Results

```bash
# See what a task did
task_runner.py --show my-task

# See all runs with full detail
task_runner.py --show my-task --all

# See tool invocations with timestamps
task_runner.py --show my-task -v

# See tool output too (verbose)
task_runner.py --show my-task -vv

# See full file content for Write/Edit operations
task_runner.py --show my-task -vvv

# Aggregate stats
task_runner.py --summary

# Monitor a running task in real-time
task_runner.py --tail my-task       # text output only
task_runner.py --tail my-task -v    # include tool invocations
task_runner.py --tail my-task -vv   # include tool output
```

`--tail` watches the subagent's live log file at
`~/.claude/projects/.../subagents/agent-{agentId}.jsonl`. Requires
`--set-agent-id` to have been called first.

Run headers in `--show` include the result value:
```
=== RUN 3 [OK: 11/11] 18 Feb 14:15 → 18 Feb 14:38 ($2.46, 38 turns, 437s) ===
=== RUN 4 [FAIL: 10/11] 18 Feb 15:00 → 18 Feb 15:22 ($1.89, 25 turns, 312s) ===
```

## Session Viewer (`format_session.py`)

Formats Claude Code session `.jsonl` logs for readable terminal output.

```bash
# List interactive sessions (excludes task runner sessions)
format_session.py --list
format_session.py --list --deleted      # Include sessions whose files were removed

# View a session by name, custom title, or ID prefix
format_session.py gnumach               # By custom title (from /rename)
format_session.py b3496f26              # By session ID prefix

# View with options
format_session.py gnumach -t            # Show timestamps
format_session.py gnumach --tools       # Show tool calls
format_session.py gnumach --tool-output # Show tool results (implies --tools)
format_session.py gnumach --thinking    # Show thinking blocks
format_session.py gnumach --all         # Show everything

# Set a display name for a session
format_session.py --name SESSION_REF "my name"
```

## Database

The database is `tasks.db` (SQLite). Key tables:

- **tasks**: Task definitions, status, prompts, dependencies, chain config.
- **runs**: One row per execution — prompt sent, output, result, log path.
- **deliverables**: Files produced by successful tasks
- **sessions**: Cache of `~/.claude` session files for `format_session.py`
  (mtime-based, tracks titles, display names, message counts)

Each run records the exact prompt that was sent (`agent_prompt` column), so you
can see what every run actually received even if the task's prompt has been
modified since.

## Architecture Notes

- **Agent tool execution**: Tasks run as Claude Code Agent tool invocations
  from within an interactive session. The orchestrating session handles the
  Agent tool call; the task runner handles everything else.
- **Auto-commit**: On success, `--complete` commits changes across all git
  repos under `~/`, excluding infrastructure files and build artifacts.
- **Run data storage**: Each run's output is stored redundantly across
  multiple locations for different purposes:

  *Old architecture* (`claude --print`, runs without `agent_id`):
  - `logs/{name}-{run_id}.log` — stream-json log (authoritative per-run record)
  - `runs.agent_output` — formatted text extracted from the log
  - Session `.jsonl` in `sessions/` — the parent `claude --print` session
    (shared across multiple runs in the same session, not per-run)

  *New architecture* (Agent tool, runs with `agent_id`):
  - Subagent `.jsonl` — the authoritative per-run record (full tool calls,
    results, usage). Hardlinked to `sessions/subagents/{sessionId}/` by
    `--complete` for backup. Claude Code may garbage-collect the original.
  - `logs/{name}-{run_id}.txt` — extracted assistant text (redundant copy)
  - `runs.agent_output` — same extracted text in the database (redundant copy)

  The redundancy exists because old runs only have stream-json logs (no
  subagent jsonl), so `logs/` and `agent_output` can't be removed without
  losing the ability to view historical runs. `--show` prefers the subagent
  log when available (richer data), falls back to `log_path` for old runs.

- **Subagent logs**: Claude Code writes subagent logs incrementally (in
  real-time) to `~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl`.
  The format is the same jsonl as regular session files (user/assistant
  messages with tool_use and tool_result blocks). `--tail` watches this
  file. The `agentId` comes from the Agent tool's result text.
- **Token accounting**: The Agent tool reports `total_tokens` in its result,
  which equals the *last* API request's `input_tokens + cache_creation_input_tokens
  + cache_read_input_tokens + output_tokens` — i.e., the conversation's ending
  size, not a sum across all requests. For detailed cost accounting, parse the
  subagent jsonl directly (each assistant message has a full `usage` object with
  per-request breakdowns). `cost_report.py` can do this.
- **Cost deduplication**: `--chat` copies the subagent log into a new session
  file and appends chat events. `/branch` forks do the same. If scanning
  multiple session files, deduplicate by event `uuid` to avoid double-counting
  tokens that were only charged once.

## Playwright MCP

If your task uses Playwright browser tools, **always call `browser_close`
as your very last Playwright action** before finishing. If you don't, the
Chromium process stays alive and the task runner hangs waiting for the
agent to exit.

## Plan Mode Redirect

If the user asks you to redirect plan mode to the task runner, add a
PreToolUse hook to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "EnterPlanMode",
        "hooks": [
          {
            "type": "command",
            "command": "echo 'BLOCKED: Do not use plan mode. Instead, write the plan to prompts/NAME and run: python3 task_runner.py --create NAME --agent TYPE' >&2; exit 2"
          }
        ]
      }
    ]
  }
}
```

When this hook is active, do NOT use `EnterPlanMode`. Instead, when a task
needs planning:

1. Write the plan to `prompts/NAME` (no extension)
2. Create the task: `task_runner.py --create NAME --agent TYPE`
3. Show the user the plan for review

(Use `--hold-on-create` here only if the user wants to review before running.)

This puts plans into the task runner where they can be reviewed, edited, and
run on demand — rather than immediately clearing context and implementing.
