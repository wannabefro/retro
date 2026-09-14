"""Store and query the probation ledger of written rules."""
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from lib.measure import measure_version

DEFAULT_PATH = os.path.expanduser("~/.retro/ledger.json")


def _resolve(path):
    return path if path else DEFAULT_PATH


def load(path=None):
    path = _resolve(path)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"rules": []}


def save(data, path=None):
    path = _resolve(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _slugify(cluster_key):
    slug = re.sub(r"[^a-z0-9]+", "-", cluster_key.lower())
    return slug.strip("-")


def _unique_id(data, base_id):
    existing = {rule["id"] for rule in data.get("rules", [])}
    if base_id not in existing:
        return base_id
    n = 2
    while f"{base_id}-{n}" in existing:
        n += 1
    return f"{base_id}-{n}"


def add(data, cluster, artifact, probation_days=14, now=None):
    now = now or datetime.now(timezone.utc)
    written_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    checkpoint = now + timedelta(days=probation_days)
    checkpoint_at = checkpoint.strftime("%Y-%m-%dT%H:%M:%SZ")
    rule_id = _unique_id(data, _slugify(cluster["key"]))
    entry = {
        "id": rule_id,
        "cluster_key": cluster["key"],
        "scope": cluster["scope"],
        "artifact": artifact,
        "written_at": written_at,
        "baseline": {
            "window_days": cluster["window_days"],
            "count": cluster["count"],
            "per_day": cluster["per_day"],
            "measure_version": measure_version(),
        },
        "checkpoint_at": checkpoint_at,
        "status": "probation",
        "result": {},
    }
    data.setdefault("rules", []).append(entry)
    return entry


def get(data, rule_id):
    for rule in data.get("rules", []):
        if rule["id"] == rule_id:
            return rule
    return None


def due(data, now=None):
    now = now or datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        rule
        for rule in data.get("rules", [])
        if rule["status"] == "probation" and rule["checkpoint_at"] <= now_str
    ]
