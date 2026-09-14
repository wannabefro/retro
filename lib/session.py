"""Summarise one finished transcript and check the ledger for a due probation checkpoint."""
import json
import os
from datetime import datetime, timezone

from lib.cluster import NOISE_CAUSES, NOISE_KINDS
from lib.extract import extract_events
from lib import ledger

NOISY_EVENT_FLOOR = 10
DEFAULT_SESSIONS_PATH = os.path.expanduser("~/.retro/sessions.jsonl")
_ALL_TIME_DAYS = 36500


def _suppressed(event):
    return event["cause"] in NOISE_CAUSES or event["kind"] in NOISE_KINDS


def profile(path):
    """Extract one transcript's events and summarise its counts, kinds, and top signatures."""
    events = list(extract_events([path], since_days=_ALL_TIME_DAYS))
    kinds = {}
    signatures = {}
    non_suppressed = 0
    for event in events:
        kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
        signatures[event["signature"]] = signatures.get(event["signature"], 0) + 1
        if not _suppressed(event):
            non_suppressed += 1
    top_signatures = sorted(signatures.items(), key=lambda kv: kv[1], reverse=True)[:5]

    if events:
        last_ts = max(event["ts"] for event in events)
        harness = events[0]["harness"]
        project = events[0]["project"]
    else:
        try:
            last_ts = os.path.getmtime(path)
        except OSError:
            last_ts = datetime.now(timezone.utc).timestamp()
        harness = ""
        project = ""

    return {
        "session": os.path.abspath(path),
        "harness": harness,
        "project": project,
        "ended_at": datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": len(events),
        "kinds": kinds,
        "top_signatures": [list(pair) for pair in top_signatures],
        "noisy": non_suppressed >= NOISY_EVENT_FLOOR,
    }


def append_profile(profile, path=None):
    """Append one profile as a JSON line, creating the sessions log's directory when absent."""
    path = path or DEFAULT_SESSIONS_PATH
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(profile) + "\n")


def due_summary(ledger_data, now=None):
    """Return the count and ids of probation entries past their checkpoint."""
    entries = ledger.due(ledger_data, now=now)
    return {"due": len(entries), "ids": [entry["id"] for entry in entries]}
