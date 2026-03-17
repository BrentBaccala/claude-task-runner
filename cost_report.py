#!/usr/bin/env python3
"""Claude API cost calculator for task runner sessions and interactive sessions.

Reports official costs where available, estimates costs for sessions without
result events, and cross-validates pricing model against official costs.

Usage:
    python3 cost_report.py                    # Summary + by-task
    python3 cost_report.py --summary          # Grand total one-liner
    python3 cost_report.py --by-task          # Cost per task
    python3 cost_report.py --by-model         # Aggregate by model
    python3 cost_report.py --by-date          # Daily cost breakdown
    python3 cost_report.py --interactive      # Interactive session costs
    python3 cost_report.py --validate         # Cross-validate pricing model
    python3 cost_report.py --all              # All reports
    python3 cost_report.py --json             # JSON output
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from project_dir import PROJECT_DIR, DB_PATH
LOGS_DIR = os.path.join(PROJECT_DIR, 'logs')
SESSIONS_DIR = os.path.expanduser("~/.claude/projects/-home-claude")

# Per million tokens, 5-minute cache rate (verified against official billing)
PRICING = {
    "claude-opus-4-6":            {"input": 5,    "output": 25,   "cache_write": 6.25,  "cache_read": 0.50},
    "claude-sonnet-4-6":          {"input": 3,    "output": 15,   "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-4-5-20250929": {"input": 3,    "output": 15,   "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5-20251001":  {"input": 1,    "output": 5,    "cache_write": 1.25,  "cache_read": 0.10},
}


def calculate_cost(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens):
    """Calculate cost from token counts using pricing table.

    Returns cost in USD, or None if model not in pricing table.
    """
    rates = PRICING.get(model)
    if not rates:
        return None
    return (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read_tokens * rates["cache_read"]
        + cache_write_tokens * rates["cache_write"]
    ) / 1_000_000


def parse_log(path):
    """Parse a stream-json log file, extracting result event data and assistant event tokens.

    Returns a dict with:
        result: dict or None — from the last result event
            total_cost_usd, num_turns, duration_ms, session_id,
            model_usage: {model: {inputTokens, outputTokens, cacheRead, cacheWrite, costUSD}}
        assistant_tokens: {model: {input, output, cache_read, cache_write, count}}
            — deduped by message ID, summed per model
        first_ts, last_ts: ISO timestamps from assistant events
    """
    if not path or not os.path.exists(path):
        return None

    result_data = None
    msg_usage = {}  # msg_id -> (model, usage_dict)
    first_ts = None
    last_ts = None

    try:
        with open(path, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "result":
                    result_data = {
                        "total_cost_usd": event.get("total_cost_usd", event.get("cost_usd")),
                        "num_turns": event.get("num_turns"),
                        "duration_ms": event.get("duration_ms"),
                        "session_id": event.get("session_id"),
                        "model_usage": {},
                    }
                    mu = event.get("modelUsage", {})
                    for model, data in mu.items():
                        result_data["model_usage"][model] = {
                            "inputTokens": data.get("inputTokens", 0),
                            "outputTokens": data.get("outputTokens", 0),
                            "cacheReadInputTokens": data.get("cacheReadInputTokens", 0),
                            "cacheCreationInputTokens": data.get("cacheCreationInputTokens", 0),
                            "costUSD": data.get("costUSD", 0),
                        }

                elif etype == "assistant":
                    msg = event.get("message", {})
                    mid = msg.get("id")
                    usage = msg.get("usage")
                    model = msg.get("model", "unknown")
                    if mid and usage and model in PRICING:
                        msg_usage[mid] = (model, usage)
                    ts = event.get("timestamp")
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
    except OSError:
        return None

    # Aggregate assistant event tokens by model
    assistant_tokens = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "count": 0
    })
    for mid, (model, usage) in msg_usage.items():
        t = assistant_tokens[model]
        t["input"] += usage.get("input_tokens", 0)
        t["output"] += usage.get("output_tokens", 0)
        t["cache_read"] += usage.get("cache_read_input_tokens", 0)
        t["cache_write"] += usage.get("cache_creation_input_tokens", 0)
        t["count"] += 1

    return {
        "result": result_data,
        "assistant_tokens": dict(assistant_tokens),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def compute_multipliers(sessions):
    """Compute per-model empirical multipliers from sessions with official costs.

    For each model, compute: median(official_cost / input_cache_cost_from_tokens).
    This gives a multiplier to scale the accurate input/cache cost to total cost,
    accounting for output tokens (which are undercounted in assistant events).

    Returns: {model: multiplier_float}
    """
    # Collect ratios per model
    ratios = defaultdict(list)  # model -> list of (official_total / input_cache_cost)

    for s in sessions:
        result = s.get("result")
        if not result or not result.get("total_cost_usd"):
            continue
        official_cost = result["total_cost_usd"]
        if official_cost <= 0:
            continue

        # Compute input/cache cost from assistant tokens
        input_cache_cost = 0
        for model, tokens in s.get("assistant_tokens", {}).items():
            rates = PRICING.get(model)
            if not rates:
                continue
            cost = (
                tokens["input"] * rates["input"]
                + tokens["cache_read"] * rates["cache_read"]
                + tokens["cache_write"] * rates["cache_write"]
            ) / 1_000_000
            input_cache_cost += cost

        if input_cache_cost > 0:
            ratio = official_cost / input_cache_cost
            # Determine the primary model (highest cost share)
            if result.get("model_usage"):
                primary_model = max(
                    result["model_usage"].items(),
                    key=lambda x: x[1].get("costUSD", 0)
                )[0]
            else:
                # Fallback: model with most tokens in assistant events
                primary_model = max(
                    s["assistant_tokens"].items(),
                    key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["cache_write"]
                )[0]
            ratios[primary_model].append(ratio)

    multipliers = {}
    for model, vals in ratios.items():
        if vals:
            multipliers[model] = statistics.median(vals)

    return multipliers


def estimate_cost(session, multipliers):
    """Estimate total cost for a session without a result event.

    Uses the input/cache tokens (which are accurate from assistant events)
    multiplied by the empirical per-model multiplier.

    Returns: (estimated_cost, primary_model)
    """
    assistant_tokens = session.get("assistant_tokens", {})
    if not assistant_tokens:
        return 0, None

    # Find the primary model
    primary_model = max(
        assistant_tokens.items(),
        key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["cache_write"]
    )[0]

    total_estimated = 0
    for model, tokens in assistant_tokens.items():
        rates = PRICING.get(model)
        if not rates:
            continue
        input_cache_cost = (
            tokens["input"] * rates["input"]
            + tokens["cache_read"] * rates["cache_read"]
            + tokens["cache_write"] * rates["cache_write"]
        ) / 1_000_000
        # Use model-specific multiplier, fall back to primary model's, then 1.2
        mult = multipliers.get(model, multipliers.get(primary_model, 1.2))
        total_estimated += input_cache_cost * mult

    return total_estimated, primary_model


def collect_task_sessions():
    """Collect session data for all task runs from the database.

    Returns list of dicts with:
        task_name, agent_type, run_id, log_path, db_cost, session_id,
        started_at, result, assistant_tokens, first_ts, last_ts
    """
    if not os.path.exists(DB_PATH):
        return []

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT r.id as run_id, r.log_path, r.cost_usd, r.session_id,
               r.started_at, r.finished_at, r.success,
               r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens,
               t.name as task_name, t.agent_type
        FROM runs r
        JOIN tasks t ON r.task_id = t.id
        ORDER BY r.id
    """).fetchall()
    db.close()

    sessions = []
    for row in rows:
        log_path = row["log_path"]
        parsed = parse_log(log_path) if log_path else None

        session = {
            "task_name": row["task_name"],
            "agent_type": row["agent_type"],
            "run_id": row["run_id"],
            "log_path": log_path,
            "db_cost": row["cost_usd"],
            "session_id": row["session_id"],
            "started_at": row["started_at"],
            "result": parsed["result"] if parsed else None,
            "assistant_tokens": parsed["assistant_tokens"] if parsed else {},
            "first_ts": parsed["first_ts"] if parsed else None,
            "last_ts": parsed["last_ts"] if parsed else None,
        }
        sessions.append(session)

    return sessions


def collect_interactive_sessions():
    """Collect session data for interactive (non-task) sessions.

    Returns list of dicts with:
        session_id, display_name, custom_title, first_ts, last_ts,
        assistant_tokens, result (always None)
    """
    if not os.path.exists(DB_PATH):
        return []

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Get task session IDs to exclude
    task_sids = set(
        r[0] for r in db.execute(
            "SELECT DISTINCT session_id FROM runs WHERE session_id IS NOT NULL"
        )
    )

    # Get interactive sessions from cache
    rows = db.execute("""
        SELECT session_id, custom_title, display_name, first_ts, last_ts
        FROM sessions
        WHERE is_task = 0 AND has_messages = 1 AND deleted = 0
        ORDER BY first_ts
    """).fetchall()
    db.close()

    sessions = []
    for row in rows:
        sid = row["session_id"]
        if sid in task_sids:
            continue

        path = os.path.join(SESSIONS_DIR, f"{sid}.jsonl")
        parsed = parse_log(path) if os.path.exists(path) else None

        session = {
            "session_id": sid,
            "display_name": row["display_name"] or row["custom_title"] or sid[:12],
            "custom_title": row["custom_title"],
            "first_ts": parsed["first_ts"] if parsed else row["first_ts"],
            "last_ts": parsed["last_ts"] if parsed else row["last_ts"],
            "assistant_tokens": parsed["assistant_tokens"] if parsed else {},
            "result": parsed["result"] if parsed else None,
        }
        sessions.append(session)

    return sessions


def format_cost(cost, estimated=False):
    """Format a cost value for display."""
    if cost is None:
        return "      -"
    prefix = "~" if estimated else " "
    return f"{prefix}${cost:>6.2f}"


def format_tokens(n):
    """Format token count with K/M suffix."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def get_date(ts):
    """Extract date string from ISO timestamp."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# ── Report formatters ──────────────────────────────────────────────────

def report_summary(task_sessions, interactive_sessions, multipliers, as_json=False):
    """Grand total summary."""
    official_cost = 0
    estimated_cost = 0
    official_count = 0
    estimated_count = 0

    for s in task_sessions:
        if s["result"] and s["result"].get("total_cost_usd"):
            official_cost += s["result"]["total_cost_usd"]
            official_count += 1
        elif s["assistant_tokens"]:
            est, _ = estimate_cost(s, multipliers)
            estimated_cost += est
            estimated_count += 1

    for s in interactive_sessions:
        if s["assistant_tokens"]:
            est, _ = estimate_cost(s, multipliers)
            estimated_cost += est
            estimated_count += 1

    total = official_cost + estimated_cost
    total_sessions = official_count + estimated_count

    if as_json:
        return {
            "total_cost": round(total, 2),
            "official_cost": round(official_cost, 2),
            "estimated_cost": round(estimated_cost, 2),
            "official_sessions": official_count,
            "estimated_sessions": estimated_count,
            "total_sessions": total_sessions,
        }

    print(f"Total cost: ${total:.2f}  "
          f"(${official_cost:.2f} official from {official_count} sessions"
          f" + ~${estimated_cost:.2f} estimated from {estimated_count} sessions)")
    return None


def report_by_task(task_sessions, multipliers, interactive_sessions=None, task_filter=None, as_json=False):
    """Cost breakdown by task, with interactive sessions section."""
    # Group by task name
    tasks = defaultdict(lambda: {
        "official_cost": 0, "estimated_cost": 0,
        "official_runs": 0, "estimated_runs": 0,
        "agent_type": None
    })

    for s in task_sessions:
        name = s["task_name"]
        if task_filter and task_filter != name:
            continue
        t = tasks[name]
        t["agent_type"] = s["agent_type"]

        if s["result"] and s["result"].get("total_cost_usd"):
            t["official_cost"] += s["result"]["total_cost_usd"]
            t["official_runs"] += 1
        elif s["assistant_tokens"]:
            est, _ = estimate_cost(s, multipliers)
            t["estimated_cost"] += est
            t["estimated_runs"] += 1

    # Sort by total cost descending
    sorted_tasks = sorted(
        tasks.items(),
        key=lambda x: x[1]["official_cost"] + x[1]["estimated_cost"],
        reverse=True
    )

    # Compute interactive session costs
    interactive_items = []
    if interactive_sessions and not task_filter:
        for s in interactive_sessions:
            if s.get("assistant_tokens"):
                est, _ = estimate_cost(s, multipliers)
                if est > 0:
                    interactive_items.append((s["display_name"], est))
        interactive_items.sort(key=lambda x: x[1], reverse=True)

    if as_json:
        result = []
        for name, t in sorted_tasks:
            result.append({
                "task": name,
                "agent_type": t["agent_type"],
                "official_cost": round(t["official_cost"], 2),
                "estimated_cost": round(t["estimated_cost"], 2),
                "total_cost": round(t["official_cost"] + t["estimated_cost"], 2),
                "official_runs": t["official_runs"],
                "estimated_runs": t["estimated_runs"],
            })
        return result

    total_official = sum(t["official_cost"] for _, t in sorted_tasks)
    total_estimated = sum(t["estimated_cost"] for _, t in sorted_tasks)
    total_runs = sum(t["official_runs"] + t["estimated_runs"] for _, t in sorted_tasks)

    print(f"\n{'Task':<40s} {'Runs':>5s} {'Official':>10s} {'Estimated':>10s} {'Total':>10s}")
    print("─" * 77)

    for name, t in sorted_tasks:
        runs = t["official_runs"] + t["estimated_runs"]
        total = t["official_cost"] + t["estimated_cost"]
        est_str = f"~${t['estimated_cost']:>6.2f}" if t["estimated_cost"] > 0 else "       -"
        off_str = f" ${t['official_cost']:>6.2f}" if t["official_cost"] > 0 else "       -"
        print(f"{name:<40s} {runs:>5d} {off_str:>10s} {est_str:>10s}  ${total:>6.2f}")

    print("─" * 77)
    task_total = total_official + total_estimated
    print(f"{'Task subtotal':<40s} {total_runs:>5d}  ${total_official:>6.2f} ~${total_estimated:>6.2f}  ${task_total:>6.2f}")

    if interactive_items:
        interactive_total = sum(cost for _, cost in interactive_items)
        print(f"\n{'Interactive Session':<40s} {'':>5s} {'':>10s} {'Estimated':>10s} {'Total':>10s}")
        print("─" * 77)
        for name, cost in interactive_items:
            print(f"{name:<40s} {'':>5s} {'':>10s} ~${cost:>6.2f}  ${cost:>6.2f}")
        print("─" * 77)
        print(f"{'Interactive subtotal':<40s} {len(interactive_items):>5d} {'':>10s} ~${interactive_total:>6.2f}  ${interactive_total:>6.2f}")

        print(f"\n{'═' * 77}")
        grand_total = task_total + interactive_total
        print(f"{'GRAND TOTAL':<40s} {'':>5s}  ${total_official:>6.2f} ~${total_estimated + interactive_total:>6.2f}  ${grand_total:>6.2f}")

    return None


def report_by_model(task_sessions, interactive_sessions, multipliers, as_json=False):
    """Aggregate cost by model with token breakdown."""
    models = defaultdict(lambda: {
        "input": 0, "output_official": 0, "output_assistant": 0,
        "cache_read": 0, "cache_write": 0,
        "official_cost": 0, "estimated_cost": 0,
        "sessions": 0
    })

    all_sessions = list(task_sessions) + list(interactive_sessions)

    for s in all_sessions:
        has_official = s.get("result") and s["result"].get("total_cost_usd")

        # Collect assistant-event tokens (always available)
        for model, tokens in s.get("assistant_tokens", {}).items():
            m = models[model]
            m["input"] += tokens["input"]
            m["output_assistant"] += tokens["output"]
            m["cache_read"] += tokens["cache_read"]
            m["cache_write"] += tokens["cache_write"]
            m["sessions"] += 1

        # Collect official data where available
        if has_official:
            for model, usage in s["result"].get("model_usage", {}).items():
                m = models[model]
                m["output_official"] += usage.get("outputTokens", 0)
                m["official_cost"] += usage.get("costUSD", 0)

    # Compute estimated costs for sessions without official data
    for s in all_sessions:
        if s.get("result") and s["result"].get("total_cost_usd"):
            continue
        if s.get("assistant_tokens"):
            est, _ = estimate_cost(s, multipliers)
            # Attribute to primary model
            primary = max(
                s["assistant_tokens"].items(),
                key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["cache_write"]
            )[0]
            models[primary]["estimated_cost"] += est

    if as_json:
        result = {}
        for model, m in sorted(models.items()):
            result[model] = {
                "input_tokens": m["input"],
                "output_tokens_official": m["output_official"],
                "output_tokens_assistant": m["output_assistant"],
                "cache_read_tokens": m["cache_read"],
                "cache_write_tokens": m["cache_write"],
                "official_cost": round(m["official_cost"], 2),
                "estimated_cost": round(m["estimated_cost"], 2),
                "total_cost": round(m["official_cost"] + m["estimated_cost"], 2),
                "sessions": m["sessions"],
            }
        return result

    print(f"\n{'Model':<30s} {'Input':>8s} {'Cache Rd':>8s} {'Cache Wr':>8s} "
          f"{'Output':>8s} {'Sessions':>8s} {'Official':>10s} {'Estim':>10s} {'Total':>10s}")
    print("─" * 104)

    grand_official = 0
    grand_estimated = 0
    for model, m in sorted(models.items()):
        total = m["official_cost"] + m["estimated_cost"]
        grand_official += m["official_cost"]
        grand_estimated += m["estimated_cost"]
        # Use official output tokens if available, otherwise assistant
        output = m["output_official"] if m["output_official"] > 0 else m["output_assistant"]
        print(f"{model:<30s} {format_tokens(m['input']):>8s} {format_tokens(m['cache_read']):>8s} "
              f"{format_tokens(m['cache_write']):>8s} {format_tokens(output):>8s} "
              f"{m['sessions']:>8d}  ${m['official_cost']:>7.2f} ~${m['estimated_cost']:>6.2f}  ${total:>7.2f}")

    print("─" * 104)
    print(f"{'TOTAL':<30s} {'':>8s} {'':>8s} {'':>8s} {'':>8s} "
          f"{'':>8s}  ${grand_official:>7.2f} ~${grand_estimated:>6.2f}  ${grand_official + grand_estimated:>7.2f}")
    return None


def report_by_date(task_sessions, interactive_sessions, multipliers, as_json=False):
    """Daily cost breakdown."""
    dates = defaultdict(lambda: {"official": 0, "estimated": 0, "sessions": 0})

    for s in task_sessions:
        date = get_date(s.get("started_at") or s.get("first_ts"))
        if not date:
            date = "unknown"
        d = dates[date]
        d["sessions"] += 1
        if s.get("result") and s["result"].get("total_cost_usd"):
            d["official"] += s["result"]["total_cost_usd"]
        elif s.get("assistant_tokens"):
            est, _ = estimate_cost(s, multipliers)
            d["estimated"] += est

    for s in interactive_sessions:
        date = get_date(s.get("first_ts"))
        if not date:
            date = "unknown"
        d = dates[date]
        d["sessions"] += 1
        if s.get("assistant_tokens"):
            est, _ = estimate_cost(s, multipliers)
            d["estimated"] += est

    sorted_dates = sorted(dates.items())

    if as_json:
        return [
            {
                "date": date,
                "official_cost": round(d["official"], 2),
                "estimated_cost": round(d["estimated"], 2),
                "total_cost": round(d["official"] + d["estimated"], 2),
                "sessions": d["sessions"],
            }
            for date, d in sorted_dates
        ]

    print(f"\n{'Date':<14s} {'Sessions':>8s} {'Official':>10s} {'Estimated':>10s} {'Total':>10s}")
    print("─" * 54)

    grand_official = 0
    grand_estimated = 0
    for date, d in sorted_dates:
        total = d["official"] + d["estimated"]
        grand_official += d["official"]
        grand_estimated += d["estimated"]
        off_str = f" ${d['official']:>6.2f}" if d["official"] > 0 else "       -"
        est_str = f"~${d['estimated']:>6.2f}" if d["estimated"] > 0 else "       -"
        print(f"{date:<14s} {d['sessions']:>8d} {off_str:>10s} {est_str:>10s}  ${total:>6.2f}")

    print("─" * 54)
    print(f"{'TOTAL':<14s} {'':>8s}  ${grand_official:>6.2f} ~${grand_estimated:>6.2f}  ${grand_official + grand_estimated:>6.2f}")
    return None


def report_interactive(interactive_sessions, multipliers, as_json=False):
    """Interactive session cost breakdown."""
    if as_json:
        result = []
        for s in interactive_sessions:
            est, model = estimate_cost(s, multipliers)
            result.append({
                "session_id": s["session_id"],
                "display_name": s["display_name"],
                "date": get_date(s.get("first_ts")),
                "model": model,
                "estimated_cost": round(est, 4),
                "tokens": s.get("assistant_tokens", {}),
            })
        return result

    if not interactive_sessions:
        print("\nNo interactive sessions found.")
        return None

    print(f"\n{'Name':<30s} {'Date':<12s} {'Model':<25s} {'Est. Cost':>10s}")
    print("─" * 79)

    total_est = 0
    for s in interactive_sessions:
        est, model = estimate_cost(s, multipliers)
        total_est += est
        date = get_date(s.get("first_ts")) or "?"
        model_short = (model or "?").replace("claude-", "").replace("-20250929", "")
        print(f"{s['display_name']:<30s} {date:<12s} {model_short:<25s} ~${est:>7.2f}")

    print("─" * 79)
    print(f"{'TOTAL':<30s} {'':>12s} {'':>25s} ~${total_est:>7.2f}")
    return None


def _session_to_json(s, multipliers, is_interactive=False):
    """Convert a session dict to JSON-serializable detail record."""
    official = None
    if s.get("result") and s["result"].get("total_cost_usd"):
        official = s["result"]["total_cost_usd"]
    est, _ = estimate_cost(s, multipliers) if s.get("assistant_tokens") else (0, None)

    run_data = {
        "date": get_date(s.get("started_at") or s.get("first_ts")),
        "official_cost": round(official, 4) if official else None,
        "estimated_cost": round(est, 4) if est else None,
        "models": {},
    }
    if is_interactive:
        run_data["session"] = s.get("display_name", s.get("session_id", "")[:12])
    else:
        run_data["run_id"] = s["run_id"]
        run_data["task"] = s["task_name"]

    # Result event tokens (the "official" token counts)
    if s.get("result") and s["result"].get("model_usage"):
        for model, usage in s["result"]["model_usage"].items():
            run_data["models"][model] = {
                "source": "result",
                "input": usage.get("inputTokens", 0),
                "output": usage.get("outputTokens", 0),
                "cache_read": usage.get("cacheReadInputTokens", 0),
                "cache_write": usage.get("cacheCreationInputTokens", 0),
                "cost": round(usage.get("costUSD", 0), 4),
            }
    # Assistant event tokens (for runs without result events)
    for model, tokens in s.get("assistant_tokens", {}).items():
        if model not in run_data["models"]:
            run_data["models"][model] = {
                "source": "assistant",
                "input": tokens["input"],
                "output": tokens["output"],
                "cache_read": tokens["cache_read"],
                "cache_write": tokens["cache_write"],
                "msgs": tokens["count"],
            }

    return run_data


def _print_session_tokens(s, multipliers):
    """Print per-model token breakdown lines for a session. Returns (official, estimated)."""
    official = None
    if s.get("result") and s["result"].get("total_cost_usd"):
        official = s["result"]["total_cost_usd"]
    est, _ = estimate_cost(s, multipliers) if s.get("assistant_tokens") else (0, None)

    # Show per-model token breakdown from the best source
    # Prefer result event tokens (they include subagent totals correctly)
    if s.get("result") and s["result"].get("model_usage"):
        for model, usage in s["result"]["model_usage"].items():
            inp = usage.get("inputTokens", 0)
            out = usage.get("outputTokens", 0)
            crd = usage.get("cacheReadInputTokens", 0)
            cwr = usage.get("cacheCreationInputTokens", 0)
            cost = usage.get("costUSD", 0)
            calc = calculate_cost(model, inp, out, crd, cwr)
            calc_str = f"  calc=${calc:.4f}" if calc is not None else ""
            model_short = model.replace("claude-", "")
            print(f"  {model_short:<28s}  in={format_tokens(inp):>6s}  "
                  f"crd={format_tokens(crd):>6s}  cwr={format_tokens(cwr):>6s}  "
                  f"out={format_tokens(out):>6s}  cost=${cost:.4f}{calc_str}")
    elif s.get("assistant_tokens"):
        for model, tokens in s["assistant_tokens"].items():
            inp = tokens["input"]
            out = tokens["output"]
            crd = tokens["cache_read"]
            cwr = tokens["cache_write"]
            ic_cost = calculate_cost(model, inp, 0, crd, cwr)
            ic_str = f"  in+cache=${ic_cost:.4f}" if ic_cost is not None else ""
            model_short = model.replace("claude-", "")
            print(f"  {model_short:<28s}  in={format_tokens(inp):>6s}  "
                  f"crd={format_tokens(crd):>6s}  cwr={format_tokens(cwr):>6s}  "
                  f"out={format_tokens(out):>6s}  ({tokens['count']} msgs){ic_str}")
    else:
        print(f"  (no token data)")

    return official, est if not official else 0


def report_detail(task_sessions, interactive_sessions, multipliers,
                  task_filter=None, as_json=False):
    """Per-run detail with token counts and both official/estimated costs.

    With --task: shows runs for that task only.
    Without --task: shows all tasks grouped by task name in chronological order
    (by first run), then interactive sessions in a separate section.
    """
    if task_filter:
        runs = [s for s in task_sessions if s["task_name"] == task_filter]
        if not runs:
            if as_json:
                return []
            print(f"\nNo runs found for task '{task_filter}'.")
            return None
        groups = [(task_filter, runs)]
        show_interactive = False
    else:
        # Group by task, ordered by first run timestamp
        task_runs = defaultdict(list)
        for s in task_sessions:
            task_runs[s["task_name"]].append(s)
        # Sort tasks by their earliest run's timestamp
        def first_ts(name):
            runs = task_runs[name]
            for r in runs:
                ts = r.get("started_at") or r.get("first_ts")
                if ts:
                    return ts
            return ""
        groups = [(name, task_runs[name]) for name in sorted(task_runs, key=first_ts)]
        show_interactive = True

    if as_json:
        result = {"tasks": [], "interactive": []}
        for name, runs in groups:
            task_data = {
                "task": name,
                "runs": [_session_to_json(s, multipliers) for s in runs],
            }
            result["tasks"].append(task_data)
        if show_interactive:
            for s in interactive_sessions:
                result["interactive"].append(
                    _session_to_json(s, multipliers, is_interactive=True)
                )
        return result

    grand_official = 0
    grand_estimated = 0

    for name, runs in groups:
        print(f"\n── {name} ({len(runs)} runs) ──\n")

        task_official = 0
        task_estimated = 0

        for s in runs:
            date = get_date(s.get("started_at") or s.get("first_ts")) or "?"
            official = None
            if s.get("result") and s["result"].get("total_cost_usd"):
                official = s["result"]["total_cost_usd"]
            est, _ = estimate_cost(s, multipliers) if s.get("assistant_tokens") else (0, None)

            off_str = f"${official:.4f}" if official else "     -"
            est_str = f"~${est:.4f}" if est else "      -"
            source = "official" if official else "estimate"
            print(f"Run {s['run_id']:<4d}  {date}  official={off_str}  estimated={est_str}  ({source})")

            off, est_cost = _print_session_tokens(s, multipliers)
            if off:
                task_official += off
            else:
                task_estimated += est_cost

        print(f"\n  Subtotal: ${task_official:.2f} official + ~${task_estimated:.2f} estimated"
              f" = ${task_official + task_estimated:.2f}")
        grand_official += task_official
        grand_estimated += task_estimated

    if show_interactive and interactive_sessions:
        print(f"\n── Interactive Sessions ({len(interactive_sessions)}) ──\n")

        int_total = 0
        for s in interactive_sessions:
            date = get_date(s.get("first_ts")) or "?"
            est, _ = estimate_cost(s, multipliers) if s.get("assistant_tokens") else (0, None)
            name = s.get("display_name", s.get("session_id", "")[:12])

            est_str = f"~${est:.4f}" if est else "      -"
            print(f"{name:<30s}  {date}  estimated={est_str}")

            _, est_cost = _print_session_tokens(s, multipliers)
            int_total += est_cost

        print(f"\n  Subtotal: ~${int_total:.2f} estimated")
        grand_estimated += int_total

    print(f"\n{'═' * 60}")
    print(f"  Grand total: ${grand_official:.2f} official + ~${grand_estimated:.2f} estimated"
          f" = ${grand_official + grand_estimated:.2f}")
    return None


def report_validate(task_sessions, as_json=False):
    """Cross-validate calculated costs against official costs.

    For each session with a result event, compare:
    - Official cost (from result event's costUSD)
    - Calculated cost (from result event's token counts * pricing table rates)
    Shows per-model error analysis.
    """
    validations = []

    for s in task_sessions:
        result = s.get("result")
        if not result or not result.get("model_usage"):
            continue

        for model, usage in result["model_usage"].items():
            official = usage.get("costUSD", 0)
            if official <= 0:
                continue

            calculated = calculate_cost(
                model,
                usage.get("inputTokens", 0),
                usage.get("outputTokens", 0),
                usage.get("cacheReadInputTokens", 0),
                usage.get("cacheCreationInputTokens", 0),
            )
            if calculated is None:
                continue

            error_pct = ((calculated - official) / official) * 100 if official > 0 else 0

            validations.append({
                "task_name": s["task_name"],
                "run_id": s["run_id"],
                "model": model,
                "official": official,
                "calculated": calculated,
                "error_pct": error_pct,
                "input": usage.get("inputTokens", 0),
                "output": usage.get("outputTokens", 0),
                "cache_read": usage.get("cacheReadInputTokens", 0),
                "cache_write": usage.get("cacheCreationInputTokens", 0),
            })

    # Aggregate by model
    model_errors = defaultdict(list)
    for v in validations:
        model_errors[v["model"]].append(v["error_pct"])

    if as_json:
        return {
            "validations": validations,
            "model_summary": {
                model: {
                    "count": len(errors),
                    "mean_error_pct": round(statistics.mean(errors), 2) if errors else 0,
                    "median_error_pct": round(statistics.median(errors), 2) if errors else 0,
                    "min_error_pct": round(min(errors), 2) if errors else 0,
                    "max_error_pct": round(max(errors), 2) if errors else 0,
                }
                for model, errors in sorted(model_errors.items())
            }
        }

    print("\n── Cross-Validation: Calculated vs Official ──")
    print(f"\nCalculated = tokens from result event × pricing table rates")
    print(f"Official = costUSD from result event\n")

    print(f"{'Model':<30s} {'Count':>6s} {'Mean Err':>10s} {'Median':>10s} {'Min':>10s} {'Max':>10s}")
    print("─" * 78)

    for model, errors in sorted(model_errors.items()):
        mean_err = statistics.mean(errors)
        med_err = statistics.median(errors)
        min_err = min(errors)
        max_err = max(errors)
        print(f"{model:<30s} {len(errors):>6d} {mean_err:>+9.2f}% {med_err:>+9.2f}% "
              f"{min_err:>+9.2f}% {max_err:>+9.2f}%")

    # Show worst mismatches
    worst = sorted(validations, key=lambda x: abs(x["error_pct"]), reverse=True)[:5]
    if worst:
        print(f"\nLargest mismatches:")
        print(f"{'Task':<35s} {'Run':>4s} {'Model':<20s} {'Official':>10s} {'Calculated':>10s} {'Error':>8s}")
        print("─" * 89)
        for v in worst:
            model_short = v["model"].replace("claude-", "")
            print(f"{v['task_name']:<35s} {v['run_id']:>4d} {model_short:<20s} "
                  f"${v['official']:>8.4f} ${v['calculated']:>8.4f} {v['error_pct']:>+7.2f}%")

    # Show multiplier analysis
    print(f"\n── Empirical Multipliers (official_cost / input_cache_cost) ──")
    print(f"\nThese are used to estimate costs for sessions without result events.")
    print(f"A multiplier of 1.15 means output costs add ~15% on top of input/cache.\n")

    # Compute multipliers inline for display
    model_ratios = defaultdict(list)
    for s in task_sessions:
        result = s.get("result")
        if not result or not result.get("total_cost_usd"):
            continue
        official_cost = result["total_cost_usd"]
        if official_cost <= 0:
            continue

        input_cache_cost = 0
        for model, tokens in s.get("assistant_tokens", {}).items():
            rates = PRICING.get(model)
            if not rates:
                continue
            cost = (
                tokens["input"] * rates["input"]
                + tokens["cache_read"] * rates["cache_read"]
                + tokens["cache_write"] * rates["cache_write"]
            ) / 1_000_000
            input_cache_cost += cost

        if input_cache_cost > 0:
            ratio = official_cost / input_cache_cost
            if result.get("model_usage"):
                primary = max(
                    result["model_usage"].items(),
                    key=lambda x: x[1].get("costUSD", 0)
                )[0]
            else:
                primary = max(
                    s["assistant_tokens"].items(),
                    key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["cache_write"]
                )[0]
            model_ratios[primary].append(ratio)

    print(f"{'Model':<30s} {'Count':>6s} {'Median':>8s} {'Mean':>8s} {'Min':>8s} {'Max':>8s}")
    print("─" * 64)
    for model, ratios in sorted(model_ratios.items()):
        print(f"{model:<30s} {len(ratios):>6d} {statistics.median(ratios):>7.3f}x "
              f"{statistics.mean(ratios):>6.3f}x {min(ratios):>6.3f}x {max(ratios):>6.3f}x")

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Claude API cost calculator for task runner and interactive sessions."
    )
    parser.add_argument("--summary", action="store_true", help="Grand total one-liner")
    parser.add_argument("--by-task", action="store_true", help="Cost per task")
    parser.add_argument("--by-model", action="store_true", help="Aggregate by model")
    parser.add_argument("--by-date", action="store_true", help="Daily cost breakdown")
    parser.add_argument("--interactive", action="store_true", help="Interactive session costs")
    parser.add_argument("--validate", action="store_true", help="Cross-validate pricing model")
    parser.add_argument("--detail", action="store_true", help="Per-run detail with token counts")
    parser.add_argument("--all", action="store_true", help="All reports")
    parser.add_argument("--task", type=str, help="Filter to a specific task name")
    parser.add_argument("--since", type=str, help="Filter to sessions since DATE (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()

    # Default: summary + by-task
    show_any = args.summary or args.by_task or args.by_model or args.by_date \
        or args.interactive or args.validate or args.detail or args.all
    if not show_any:
        args.summary = True
        args.by_task = True

    if args.all:
        args.summary = args.by_task = args.by_model = args.by_date = True
        args.interactive = args.validate = True

    # Collect data
    task_sessions = collect_task_sessions()
    interactive_sessions = collect_interactive_sessions()

    # Apply --since filter
    if args.since:
        task_sessions = [
            s for s in task_sessions
            if (get_date(s.get("started_at") or s.get("first_ts")) or "") >= args.since
        ]
        interactive_sessions = [
            s for s in interactive_sessions
            if (get_date(s.get("first_ts")) or "") >= args.since
        ]

    # Compute multipliers from sessions with official costs
    multipliers = compute_multipliers(task_sessions)

    if args.json:
        output = {}
        if args.summary:
            output["summary"] = report_summary(task_sessions, interactive_sessions, multipliers, as_json=True)
        if args.by_task:
            output["by_task"] = report_by_task(task_sessions, multipliers, task_filter=args.task, as_json=True)
        if args.by_model:
            output["by_model"] = report_by_model(task_sessions, interactive_sessions, multipliers, as_json=True)
        if args.by_date:
            output["by_date"] = report_by_date(task_sessions, interactive_sessions, multipliers, as_json=True)
        if args.interactive:
            output["interactive"] = report_interactive(interactive_sessions, multipliers, as_json=True)
        if args.validate:
            output["validate"] = report_validate(task_sessions, as_json=True)
        if args.detail:
            output["detail"] = report_detail(task_sessions, interactive_sessions, multipliers, task_filter=args.task, as_json=True)
        output["multipliers"] = {k: round(v, 4) for k, v in multipliers.items()}
        print(json.dumps(output, indent=2))
        return

    # Text reports
    if args.summary:
        report_summary(task_sessions, interactive_sessions, multipliers)
    if args.by_task:
        report_by_task(task_sessions, multipliers, interactive_sessions=interactive_sessions, task_filter=args.task)
    if args.by_model:
        report_by_model(task_sessions, interactive_sessions, multipliers)
    if args.by_date:
        report_by_date(task_sessions, interactive_sessions, multipliers)
    if args.interactive:
        report_interactive(interactive_sessions, multipliers)
    if args.detail:
        report_detail(task_sessions, interactive_sessions, multipliers, task_filter=args.task)
    if args.validate:
        report_validate(task_sessions)


if __name__ == "__main__":
    main()
