#!/usr/bin/env python3
"""Report which tasks have written compactions and lightrag KG entries.

For each task, looks up the parent session(s) that ran it (via the
subagent-dir layout) and checks whether the corresponding compaction
and KG-entry artifacts exist. A task is "covered" if AT LEAST ONE of
its runs landed in a parent session that produced the artifact.

Sources:
  ~/project/tasks.db                                — tasks + runs
  ~/project/sessions/subagents/<parent>/agent-<id>  — agent_id → parent
  ~/project/compactions/session-<UUID>.md           — compactions
  ~/lightrag-history/{incoming,processed,failed}/
      session-<UUID>.json                           — KG entries

Coverage states per artifact (compaction, KG):
  ok          — at least one of the task's parent sessions has it
  missing     — none do, but at least one parent session is identified
  unmappable  — no run has agent_id, or no agent_id maps to a subagent dir

Usage:
  coverage_report.py                       # full table + summary
  coverage_report.py --uncovered           # only rows missing compaction OR KG
  coverage_report.py --no-compaction       # only rows missing the compaction
  coverage_report.py --no-kg               # only rows missing the KG entry
  coverage_report.py --task-status ok      # only tasks with status=ok
  coverage_report.py --json                # machine-readable output
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

SESSION_UUID_RE = re.compile(
    r'\*\*Session\*\*:\s*'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
    re.IGNORECASE,
)

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from project_dir import PROJECT_DIR, DB_PATH

SUBAGENTS_DIR = Path(PROJECT_DIR) / "sessions" / "subagents"
COMPACTIONS_DIR = Path(PROJECT_DIR) / "compactions"
SESSIONS_DIR = Path(PROJECT_DIR) / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
LIGHTRAG_DIRS = [
    Path.home() / "lightrag-history" / "incoming",
    Path.home() / "lightrag-history" / "processed",
    Path.home() / "lightrag-history" / "failed",
]


def canonical_session_id(file_uuid: str, _cache: dict = {}) -> str:
    """Return the canonical sessionId from the JSONL named <file_uuid>.jsonl.

    Dereferences `--chat`-style session forks: a forked session's JSONL
    has a new filename UUID but its events still carry the original
    parent's sessionId. Returns file_uuid unchanged if no JSONL is
    found or if no event in it has a sessionId.

    Cached per-process — each JSONL is read at most once per run.
    """
    if file_uuid in _cache:
        return _cache[file_uuid]
    candidates = [
        SESSIONS_DIR / f"{file_uuid}.jsonl",
        CLAUDE_PROJECTS / "-home-claude" / f"{file_uuid}.jsonl",
    ]
    candidates += list(CLAUDE_PROJECTS.glob(f"*/{file_uuid}.jsonl"))
    for p in candidates:
        if not p.is_file():
            continue
        try:
            with open(p) as f:
                for i, line in enumerate(f):
                    if i > 20:  # paranoia bound; sessionId is on event 1
                        break
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    sid = ev.get("sessionId")
                    if sid:
                        _cache[file_uuid] = sid
                        return sid
        except Exception:
            continue
    _cache[file_uuid] = file_uuid
    return file_uuid


def build_agent_to_parent_map() -> dict[str, str]:
    """Walk subagents/<parent>/agent-<aid>.jsonl → {aid: parent}."""
    m: dict[str, str] = {}
    if not SUBAGENTS_DIR.is_dir():
        return m
    for parent_dir in SUBAGENTS_DIR.iterdir():
        if not parent_dir.is_dir():
            continue
        parent = parent_dir.name
        for f in parent_dir.glob("agent-*.jsonl"):
            aid = f.stem.removeprefix("agent-")
            m[aid] = parent
    return m


def build_canonical_to_filenames(filenames: set[str]) -> dict[str, set[str]]:
    """Reverse map {canonical_sessionId: {filename_uuids that resolve here}}.
    Used for KG entries (uniform session-<UUID>.json convention)."""
    out: dict[str, set[str]] = defaultdict(set)
    for f in filenames:
        out[canonical_session_id(f)].add(f)
    return out


def compaction_canonical(path: Path) -> str:
    """Resolve a compaction file's canonical session UUID.

    Two filename conventions live in compactions/:
      session-<UUID>.md       (modern /end skill) — UUID dereferenced
                              via the matching JSONL's first-event sessionId
      DDMonYYYY-HHMM.md       (older /compaction skill) — body's
                              `**Session**: <UUID>` line is the canonical id

    Falls back to the basename if no canonical UUID can be derived
    (some older compactions have no Session line).
    """
    base = path.stem
    if base.startswith("session-"):
        return canonical_session_id(base.removeprefix("session-"))
    try:
        head = path.read_text(errors="replace")[:4000]
    except Exception:
        return base
    m = SESSION_UUID_RE.search(head)
    return m.group(1) if m else base


_WRITE_TOOL_RE = re.compile(
    r'(?:Write|Edit|MultiEdit|NotebookEdit)\(\s*(?:file_path|notebook_path)'
    r'\s*:\s*"([^"]+)"'
)


def probe_task_writes(task_id: int, candidate_basenames: set[str],
                      task_runner_bin: str = "task_runner.py") -> set[str]:
    """Run `task_runner.py --show <id> -v`, extract Write/Edit tool-call
    file_path arguments, and return the candidate basenames found in any
    of them.

    Strict: only tool-use Write/Edit/MultiEdit/NotebookEdit calls count.
    Mentions in committed_files listings (`A compactions/...`) or in
    narrative prose don't — those don't prove this run wrote the file
    (a sibling subagent under the same orchestrator may have, with the
    parent's commit step sweeping it up).
    """
    if not candidate_basenames:
        return set()
    try:
        res = subprocess.run(
            [task_runner_bin, "--show", str(task_id), "-v"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    text = res.stdout + res.stderr
    written_paths = _WRITE_TOOL_RE.findall(text)
    found: set[str] = set()
    for path in written_paths:
        for base in candidate_basenames:
            if base in path:
                found.add(base)
    return found


def build_artifact_sets(dirs: list[Path], pattern: str) -> tuple[set[str], set[str]]:
    """Return (filename_uuids, canonical_session_ids) for artifact files.

    filename_uuids are the literal UUIDs from the filenames — used for
    direct per-run attribution via runs.chat_session_id (a chat-resumed
    session writes session-<chat_session_id>.<ext>).

    canonical_session_ids are the dereferenced sessionIds from each
    file's matching JSONL — used for parent-shared coverage (a session
    that orchestrated tasks may have a single compaction covering all
    of them).
    """
    filenames: set[str] = set()
    canonical: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob(pattern):
            file_uuid = f.stem.removeprefix("session-")
            filenames.add(file_uuid)
            canonical.add(canonical_session_id(file_uuid))
    return filenames, canonical


def build_compaction_sets() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Returns (filename_uuids, canonical_ids, canonical_to_basenames).

    filename_uuids: bare UUIDs from session-<UUID>.md filenames (no
        'session-' prefix). Used for chat_session_id direct-attribution
        matching. Older DDMonYYYY-HHMM.md files contribute nothing here
        — there's no UUID in their filename to pin a chat_session_id to.

    canonical_ids: per-file canonical session UUID, regardless of
        filename convention. Modern files dereference via JSONL first
        event; older files parse `**Session**: <UUID>` from the body.
        Used for parent-shared coverage matching.

    canonical_to_basenames: reverse map {canonical_id -> {full basenames}}.
        Used by the probe — greps the verbose run output for these
        strings, which match Write tool-use lines verbatim.
    """
    if not COMPACTIONS_DIR.is_dir():
        return set(), set(), {}
    filename_uuids: set[str] = set()
    canonical: set[str] = set()
    rev: dict[str, set[str]] = defaultdict(set)
    for f in COMPACTIONS_DIR.glob("*.md"):
        cid = compaction_canonical(f)
        canonical.add(cid)
        rev[cid].add(f.stem)
        if f.stem.startswith("session-"):
            filename_uuids.add(f.stem.removeprefix("session-"))
    return filename_uuids, canonical, rev


def build_kg_sets() -> tuple[set[str], set[str]]:
    return build_artifact_sets(LIGHTRAG_DIRS, "session-*.json")


def load_task_runs(conn) -> dict[int, list[dict]]:
    """task_id -> [{agent_id, session_id, chat_session_id, success, started_at}, ...]."""
    out: dict[int, list[dict]] = defaultdict(list)
    cur = conn.execute(
        "SELECT task_id, agent_id, session_id, chat_session_id, success, started_at "
        "FROM runs ORDER BY started_at"
    )
    for task_id, agent_id, session_id, chat_session_id, success, started_at in cur:
        out[task_id].append({
            "agent_id":        agent_id,
            "session_id":      session_id,
            "chat_session_id": chat_session_id,
            "success":         success,
            "started_at":      started_at,
        })
    return out


def coverage_for_task(
    runs: list[dict],
    agent_to_parent: dict[str, str],
    comp_filenames: set[str], comp_canonical: set[str],
    kg_filenames: set[str],   kg_canonical: set[str],
) -> dict:
    """Compute coverage for one task's runs.

    Two coverage tiers per artifact:
      direct  — a run's chat_session_id matches an artifact filename
                (definitively this run wrote it via end2/--chat-resume)
      shared  — a parent session has the artifact (could have been
                written by the orchestrator or any sibling subagent;
                no per-task attribution)

    Parent UUID resolution: older runs have runs.session_id set to the
    parent UUID directly; newer runs have runs.agent_id looked up via
    subagents/<parent>/agent-<id>.jsonl. Mutually exclusive in practice.
    """
    parents = []
    direct_comp = direct_kg = False
    for r in runs:
        csid = r.get("chat_session_id")
        if csid and csid in comp_filenames:
            direct_comp = True
        if csid and csid in kg_filenames:
            direct_kg = True

        sid = r.get("session_id")
        if sid:
            if sid not in parents:
                parents.append(sid)
            continue
        aid = r.get("agent_id")
        if aid and aid in agent_to_parent:
            p = agent_to_parent[aid]
            if p not in parents:
                parents.append(p)

    if not runs:
        comp = kg = "unmappable"
    elif not parents:
        comp = "ok" if direct_comp else "unmappable"
        kg = "ok" if direct_kg else "unmappable"
    else:
        if direct_comp:
            comp = "ok"
        elif any(p in comp_canonical for p in parents):
            comp = "shared"
        else:
            comp = "missing"
        if direct_kg:
            kg = "ok"
        elif any(p in kg_canonical for p in parents):
            kg = "shared"
        else:
            kg = "missing"

    return {"parents": parents, "compaction": comp, "kg": kg}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--uncovered", action="store_true",
                    help="show only rows missing compaction OR KG")
    ap.add_argument("--no-compaction", action="store_true",
                    help="show only rows missing the compaction")
    ap.add_argument("--no-kg", action="store_true",
                    help="show only rows missing the KG entry")
    ap.add_argument("--task-status", default=None,
                    help="filter to tasks with this status "
                         "(e.g. completed, failed, hold, pending)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="emit JSON instead of a table")
    ap.add_argument("--no-probe-shared", action="store_true",
                    help="don't probe `task_runner.py --show -v` to upgrade "
                         "shared verdicts to ok when the verbose run trace "
                         "shows the task wrote one of its parent's artifact "
                         "files (probe runs ~0.1s per shared task)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    agent_to_parent = build_agent_to_parent_map()
    comp_filenames, comp_canonical, comp_canon_to_files = build_compaction_sets()
    kg_filenames, kg_canonical = build_kg_sets()
    kg_canon_to_files = build_canonical_to_filenames(kg_filenames)
    runs_by_task = load_task_runs(conn)

    tasks_q = "SELECT id, name, status FROM tasks ORDER BY id"
    rows = []
    for t in conn.execute(tasks_q):
        if args.task_status and t["status"] != args.task_status:
            continue
        runs = runs_by_task.get(t["id"], [])
        cov = coverage_for_task(runs, agent_to_parent,
                                comp_filenames, comp_canonical,
                                kg_filenames,   kg_canonical)

        # Probe shared verdicts: if the task's verbose --show output
        # mentions any of its parent's artifact filenames, upgrade to ok.
        if not args.no_probe_shared:
            if cov["compaction"] == "shared":
                candidates: set[str] = set()
                for p in cov["parents"]:
                    candidates |= comp_canon_to_files.get(p, set())
                if probe_task_writes(t["id"], candidates):
                    cov["compaction"] = "ok"
            if cov["kg"] == "shared":
                candidates = set()
                for p in cov["parents"]:
                    candidates |= kg_canon_to_files.get(p, set())
                if probe_task_writes(t["id"], candidates):
                    cov["kg"] = "ok"

        rows.append({
            "task_id":    t["id"],
            "name":       t["name"],
            "status":     t["status"],
            "n_runs":     len(runs),
            "parents":    cov["parents"],
            "compaction": cov["compaction"],
            "kg":         cov["kg"],
        })

    if args.uncovered:
        rows = [r for r in rows if r["compaction"] != "ok" or r["kg"] != "ok"]
    if args.no_compaction:
        rows = [r for r in rows if r["compaction"] != "ok"]
    if args.no_kg:
        rows = [r for r in rows if r["kg"] != "ok"]

    if args.as_json:
        print(json.dumps(rows, indent=2))
        return

    # Summary
    total = len(rows)
    by_state = defaultdict(int)
    for r in rows:
        by_state[("comp", r["compaction"])] += 1
        by_state[("kg",   r["kg"])]         += 1
    print(f"Tasks shown: {total}")
    print(f"  compaction: ok={by_state[('comp','ok')]:4d}  "
          f"shared={by_state[('comp','shared')]:4d}  "
          f"missing={by_state[('comp','missing')]:4d}  "
          f"unmappable={by_state[('comp','unmappable')]:4d}")
    print(f"  KG entry  : ok={by_state[('kg','ok')]:4d}  "
          f"shared={by_state[('kg','shared')]:4d}  "
          f"missing={by_state[('kg','missing')]:4d}  "
          f"unmappable={by_state[('kg','unmappable')]:4d}")
    print("  ok     = direct attribution (chat_session_id match, or --show -v "
          "tool-trace shows the task wrote the file)")
    print("  shared = parent session has the artifact, but no per-task attribution")
    print(f"  parent sessions known: "
          f"{sum(1 for r in rows if r['parents'])} / {total}")
    print()

    # Table: id | status | C | K | n_runs | parent (first) | name
    header = f"{'ID':>4} {'STATUS':<8} {'C':<4} {'K':<4} {'RUNS':>4} {'PARENT':<10} NAME"
    print(header)
    print("-" * len(header))
    sym = {"ok": "ok", "shared": "~", "missing": "-", "unmappable": "?"}
    for r in rows:
        parent = r["parents"][0][:8] if r["parents"] else ""
        if len(r["parents"]) > 1:
            parent += f"+{len(r['parents'])-1}"
        print(f"{r['task_id']:>4} {(r['status'] or ''):<8.8} "
              f"{sym[r['compaction']]:<4} {sym[r['kg']]:<4} "
              f"{r['n_runs']:>4} {parent:<10} {r['name']}")


if __name__ == "__main__":
    main()
