"""Group failure events into ranked, threshold-gated clusters."""

NOISE_CAUSES = frozenset({"test or build failure", "expected exit"})
NOISE_KINDS = frozenset({"retry_identical", "compaction"})
# "permission denied by user" says only that something was refused, never what: as uninformative as "other".
UNINFORMATIVE_CAUSES = frozenset({"other", "permission denied by user"})


def cluster_key(event):
    """Build the group key, adding the signature only when cause carries no information."""
    base = f"{event['kind']}:{event['tool']}:{event['cause']}"
    if event["cause"] in UNINFORMATIVE_CAUSES:
        return f"{base}:{event['signature']}"
    return base


def _scope_for(projects):
    if len(projects) >= 2:
        return "global"
    if len(projects) == 1:
        return f"project:{projects[0]}"
    return "global"


def cluster_all(events, window_days=30, min_count=5, min_sessions=3, drop_noise=True):
    groups = {}
    for event in events:
        if drop_noise and (event["cause"] in NOISE_CAUSES or event["kind"] in NOISE_KINDS):
            continue
        key = cluster_key(event)
        group = groups.setdefault(key, {
            "kind": event["kind"],
            "tool": event["tool"],
            "cause": event["cause"],
            "signature": event["signature"] if event["cause"] in UNINFORMATIVE_CAUSES else "",
            "sessions": set(),
            "projects": set(),
            "harnesses": set(),
            "details": set(),
            "count": 0,
        })
        group["count"] += 1
        group["sessions"].add(event["session"])
        if event["project"]:
            group["projects"].add(event["project"])
        group["harnesses"].add(event["harness"])
        group["details"].add(event["detail"])

    passed = []
    rejected = []
    for key, group in groups.items():
        projects = sorted(group["projects"])
        examples = sorted(group["details"], key=lambda d: (len(d), d))[:3]
        record = {
            "key": key,
            "kind": group["kind"],
            "tool": group["tool"],
            "cause": group["cause"],
            "signature": group["signature"],
            "count": group["count"],
            "sessions": len(group["sessions"]),
            "projects": projects,
            "harnesses": sorted(group["harnesses"]),
            "per_day": round(group["count"] / window_days, 2),
            "window_days": window_days,
            "scope": _scope_for(projects),
            "examples": examples,
        }
        if record["count"] >= min_count and record["sessions"] >= min_sessions:
            passed.append(record)
        else:
            rejected.append(record)

    passed.sort(key=lambda r: r["count"], reverse=True)
    rejected.sort(key=lambda r: r["count"], reverse=True)
    return passed, rejected


def cluster(events, window_days=30, min_count=5, min_sessions=3, drop_noise=True):
    passed, _ = cluster_all(events, window_days, min_count, min_sessions, drop_noise)
    return passed


def promote(existing_scope, cluster_record):
    projects = cluster_record["projects"]
    if existing_scope != "global" and len(projects) >= 2:
        return "global"
    if existing_scope == "global" and len(projects) == 1:
        return f"project:{projects[0]}"
    return existing_scope
