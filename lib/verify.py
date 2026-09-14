"""Judge a probationary rule against a fresh measurement of its own baseline."""
from lib.ledger import get

KEEP_THRESHOLD = -0.25


def verdict(entry, current):
    baseline_per_day = entry["baseline"]["per_day"]
    current_per_day = current["per_day"]

    if baseline_per_day == 0:
        return {
            "status": "reverted",
            "delta_pct": 0.0,
            "baseline_per_day": baseline_per_day,
            "current_per_day": current_per_day,
            "reason": "The baseline per-day rate was 0, so the rule targeted nothing.",
        }

    if current_per_day == 0:
        return {
            "status": "kept",
            "delta_pct": -1.0,
            "baseline_per_day": baseline_per_day,
            "current_per_day": current_per_day,
            "reason": f"The rate dropped from {baseline_per_day}/day to 0/day.",
        }

    delta_pct = round((current_per_day - baseline_per_day) / baseline_per_day, 3)
    status = "kept" if delta_pct <= KEEP_THRESHOLD else "reverted"
    return {
        "status": status,
        "delta_pct": delta_pct,
        "baseline_per_day": baseline_per_day,
        "current_per_day": current_per_day,
        "reason": (
            f"The rate moved from {baseline_per_day}/day to {current_per_day}/day "
            f"({delta_pct:+.1%})."
        ),
    }


def apply_verdict(data, rule_id, current, now=None):
    entry = get(data, rule_id)
    if entry is None:
        raise ValueError(f"no such rule: {rule_id}")
    result = verdict(entry, current)
    entry["result"] = result
    entry["status"] = result["status"]
    return entry
