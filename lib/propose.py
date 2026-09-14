"""Build judgement briefs for a reasoning agent, and gate the record refusals."""
import os
from collections import Counter, defaultdict
from itertools import zip_longest

from lib.cluster import cluster_key
from lib.report import MAX_LINE

MAX_SAMPLES = 12

PROTECTED_PATTERNS = (
    "policy-limits.json",
    "settings.json",
    "settings.local.json",
    "managed-settings.json",
)

_PROTECTED_SUBSTRINGS = ("safety", "boundary")


def is_protected_path(path):
    """Refuse a resolved path matching a protected filename or a safety/boundary basename."""
    basename = os.path.basename(os.path.abspath(os.path.expanduser(path))).lower()
    if basename in PROTECTED_PATTERNS:
        return True
    return any(term in basename for term in _PROTECTED_SUBSTRINGS)


def _where_for_scope(scope):
    """Name the directory a rule for this scope belongs in, or say why that fails."""
    if scope == "global":
        return os.path.expanduser("~/.claude"), None
    reason = (
        f"session data carries only the project name for {scope!r}, never its "
        "filesystem path; find the checkout yourself and pass it to `retro record`"
    )
    return None, reason


def _samples(cluster_events):
    """Pick up to MAX_SAMPLES events round-robin across sessions, so no single burst dominates."""
    groups = defaultdict(list)
    session_order = []
    for event in cluster_events:
        session = event["session"]
        if session not in groups:
            session_order.append(session)
        groups[session].append(event)

    selected = []
    for row in zip_longest(*(groups[session] for session in session_order)):
        for event in row:
            if event is not None:
                selected.append(event)
                if len(selected) == MAX_SAMPLES:
                    break
        if len(selected) == MAX_SAMPLES:
            break

    return [
        {
            "detail": event["detail"],
            "signature": event["signature"],
            "harness": event["harness"],
            "session": os.path.basename(event["session"]),
        }
        for event in selected
    ]


def _signature_breakdown(cluster_events):
    return dict(Counter(event["signature"] for event in cluster_events))


def _skip_reason(record, scope, probation_keys, reverted_keys):
    if record["key"] in probation_keys:
        return "already on probation"
    if record["key"] in reverted_keys:
        return "previously reverted; a disproved rule is never re-proposed"
    if scope is not None and record["scope"] != scope:
        return f"scope {record['scope']} does not match requested scope {scope}"
    return None


def build_briefs(events, clusters, ledger_data, limit, scope=None):
    """Select the highest-count eligible clusters and hand over their raw events."""
    rules = (ledger_data or {}).get("rules", [])
    probation_keys = {rule["cluster_key"] for rule in rules if rule["status"] == "probation"}
    reverted_keys = {rule["cluster_key"] for rule in rules if rule["status"] == "reverted"}

    events_by_key = defaultdict(list)
    for event in events:
        events_by_key[cluster_key(event)].append(event)

    eligible = []
    skipped = []
    for record in clusters:
        reason = _skip_reason(record, scope, probation_keys, reverted_keys)
        if reason:
            skipped.append({"key": record["key"], "reason": reason})
        else:
            eligible.append(record)

    eligible.sort(key=lambda r: r["count"], reverse=True)

    briefs = []
    for record in eligible[:limit]:
        cluster_events = events_by_key.get(record["key"], [])
        where, where_reason = _where_for_scope(record["scope"])
        briefs.append({
            "cluster": record,
            "samples": _samples(cluster_events),
            "signatures": _signature_breakdown(cluster_events),
            "where": where,
            "where_reason": where_reason,
        })
    return briefs, skipped


def _truncate(text, width):
    """Cut text to width, marking the cut with an ellipsis when it loses content."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _single_line(text):
    """Collapse embedded newlines so one event never spans two rendered lines."""
    return " ".join(text.split())


def format_briefs(briefs, skipped):
    """Render briefs and skip reasons as plain text, every line under the line budget."""
    lines = []
    if not briefs:
        lines.append("No eligible clusters passed the gate.")
    for i, brief in enumerate(briefs, 1):
        record = brief["cluster"]
        prefix = f"{i}) "
        suffix = (
            f"  count={record['count']} per_day={record['per_day']} "
            f"sessions={record['sessions']} scope={record['scope']}"
        )
        key_budget = max(MAX_LINE - len(prefix) - len(suffix), 10)
        lines.append(prefix + _truncate(record["key"], key_budget) + suffix)
        where = brief["where"] or f"unresolved ({brief['where_reason']})"
        lines.append(_truncate(f"   where: {where}", MAX_LINE))
        lines.append(f"   signatures ({len(brief['signatures'])} distinct):")
        for sig, count in brief["signatures"].items():
            sig_text = _single_line(sig) or "(none)"
            lines.append(_truncate(f"     - {count}x {sig_text}", MAX_LINE))
        lines.append(f"   samples ({len(brief['samples'])} of {record['count']}):")
        for sample in brief["samples"]:
            prefix = f"     - [{sample['harness']}] {sample['session']}: "
            budget = max(MAX_LINE - len(prefix), 10)
            lines.append(prefix + _truncate(_single_line(sample["detail"]), budget))
    if skipped:
        lines.append("")
        lines.append(f"Skipped {len(skipped)} cluster(s):")
        for item in skipped:
            lines.append(_truncate(f"  - {item['key']}: {item['reason']}", MAX_LINE))
    return "\n".join(lines)
