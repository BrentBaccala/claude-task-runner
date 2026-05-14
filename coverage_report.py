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
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

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


def build_compaction_sets() -> tuple[set[str], set[str]]:
    return build_artifact_sets([COMPACTIONS_DIR], "session-*.md")


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
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    agent_to_parent = build_agent_to_parent_map()
    comp_filenames, comp_canonical = build_compaction_sets()
    kg_filenames, kg_canonical = build_kg_sets()
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
    print("  ok     = direct attribution (run.chat_session_id matches an artifact filename)")
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
