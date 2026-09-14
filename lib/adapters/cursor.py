"""Cursor agent-transcript JSONL: role-tagged lines with no type key, no tool results."""
import json
import os


def detect(first_lines):
    for line in first_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("role") in ("user", "assistant") and "type" not in obj:
            return True
    return False


def _text_of(message):
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if isinstance(content, str):
        return content
    return ""


def events(path, since_ts):
    # Cursor records no tool results, so only user_interrupt and compaction ever fire here.
    from lib.extract import redact, resolve_project

    path = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    if mtime < since_ts:
        return

    project = ""

    with open(path, "r", errors="replace") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue

            if not project:
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    project = resolve_project(cwd)

            if obj.get("isCompactSummary"):
                text = _text_of(obj.get("message", {}))
                yield {
                    "session": path, "harness": "cursor", "project": project,
                    "ts": mtime, "kind": "compaction", "tool": "", "cause": "other",
                    "detail": redact(text[:200]), "signature": "(none)",
                }
                continue

            if obj.get("role") != "user":
                continue
            text = _text_of(obj.get("message", {}))
            if "[Request interrupted by user" in text:
                yield {
                    "session": path, "harness": "cursor", "project": project,
                    "ts": mtime, "kind": "user_interrupt", "tool": "", "cause": "other",
                    "detail": redact(text[:200]), "signature": "(none)",
                }
