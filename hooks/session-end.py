#!/usr/bin/env python3
"""SessionEnd hook: profile the ending transcript and append it. Never blocks the session."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.session import append_profile, profile


def _debug(message):
    if os.environ.get("RETRO_DEBUG"):
        print(message, file=sys.stderr)


def _find_by_session_id(session_id):
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return None
    matches = list(root.rglob(f"{session_id}.jsonl"))
    return matches[0] if matches else None


def _resolve_transcript(payload):
    """Prefer transcript_path; fall back to session_id resolved under ~/.claude/projects."""
    path = payload.get("transcript_path")
    if isinstance(path, str) and path:
        return path
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        found = _find_by_session_id(session_id)
        if found:
            return str(found)
    return None


def main(stdin=None):
    stdin = stdin if stdin is not None else sys.stdin
    raw = stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError) as exc:
        _debug(f"invalid json payload: {exc}")
        return
    if not isinstance(payload, dict):
        return
    path = _resolve_transcript(payload)
    if not path or not os.path.isfile(path):
        return
    append_profile(profile(path))


def run(stdin=None):
    try:
        main(stdin=stdin)
    except Exception as exc:
        _debug(f"session-end failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
