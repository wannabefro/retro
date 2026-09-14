"""Codex CLI session JSONL: payload-typed lines, item_completed status carries the outcome."""
import json
import os
from datetime import datetime


def detect(first_lines):
    for line in first_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if "payload" in obj:
            return True
    return False


def _parse_ts(obj, mtime):
    ts = obj.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return mtime
    return mtime


def _canonical_args(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw[:500]
    return json.dumps(raw, sort_keys=True)[:500]


def _item_text(item):
    for key in ("stderr", "aggregated_output", "formatted_output", "stdout"):
        val = item.get(key)
        if val:
            return val
    result = item.get("result")
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        if isinstance(content, str):
            return content
    return json.dumps(item)[:500]


def _shell_line(raw):
    """Reduce a command field (string, or ['/bin/zsh', '-lc', line]) to the invoked line."""
    if isinstance(raw, list):
        if len(raw) >= 2 and any(a in ("-c", "-lc") for a in raw[:-1]):
            return str(raw[-1])
        return " ".join(str(x) for x in raw)
    if isinstance(raw, str):
        return raw
    return ""


def events(path, since_ts):
    from lib.extract import SHELL_TOOLS, binary_of, classify_and_sign, redact, resolve_project

    path = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0

    project = ""
    prev_call = None
    prev_call_id = None
    call_status = {}  # call_id -> bool, did that call's completed item come back an error

    with open(path, "r", errors="replace") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue

            payload = obj.get("payload")
            ts = _parse_ts(obj, mtime)

            if isinstance(payload, dict) and not project:
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    project = resolve_project(cwd)

            if obj.get("type") == "compacted":
                detail = redact(str((payload or {}).get("message", ""))[:200])
                if ts >= since_ts:
                    yield {
                        "session": path, "harness": "codex", "project": project,
                        "ts": ts, "kind": "compaction", "tool": "", "cause": "other",
                        "detail": detail, "signature": "(none)",
                    }
                continue

            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")

            if ptype == "turn_aborted" and payload.get("reason") == "interrupted":
                if ts >= since_ts:
                    yield {
                        "session": path, "harness": "codex", "project": project,
                        "ts": ts, "kind": "user_interrupt", "tool": "", "cause": "other",
                        "detail": "", "signature": "(none)",
                    }
                continue

            if ptype == "message" and payload.get("role") == "user":
                content = payload.get("content")
                text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict)) if isinstance(content, list) else ""
                if "[Request interrupted by user" in text and ts >= since_ts:
                    yield {
                        "session": path, "harness": "codex", "project": project,
                        "ts": ts, "kind": "user_interrupt", "tool": "", "cause": "other",
                        "detail": redact(text[:200]), "signature": "(none)",
                    }
                continue

            if ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name", "")
                call_id = payload.get("call_id")
                raw_args = payload.get("arguments", payload.get("input", {}))
                canonical = _canonical_args(raw_args)
                current_call = (name, canonical)
                if prev_call is not None and current_call == prev_call and ts >= since_ts:
                    prev_errored = call_status.get(prev_call_id, False)
                    kind = "retry_after_error" if prev_errored else "retry_identical"
                    yield {
                        "session": path, "harness": "codex", "project": project,
                        "ts": ts, "kind": kind, "tool": name,
                        "cause": "other", "detail": redact(canonical)[:200],
                        "signature": "(none)",
                    }
                prev_call = current_call
                prev_call_id = call_id
                continue

            if ptype == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict):
                    continue
                status = item.get("status")
                item_id = item.get("id")
                if item_id is not None:
                    call_status[item_id] = status in ("failed", "declined")
                if status not in ("failed", "declined"):
                    continue
                tool = item.get("item_type") or item.get("type") or ""
                detail_raw = _item_text(item)
                if status == "declined":
                    cause = "permission denied by user"
                    kind = "permission_denied"
                    signature = redact(detail_raw)[:60].strip() or "(none)"
                else:
                    is_shell = tool in SHELL_TOOLS
                    exit_code = item.get("exit_code") if is_shell else None
                    binary = binary_of(_shell_line(item.get("command"))) if is_shell else ""
                    cause, signature = classify_and_sign(tool, detail_raw, exit_code=exit_code, binary=binary)
                    kind = "permission_denied" if cause == "permission denied by user" else "tool_error"
                if ts >= since_ts:
                    yield {
                        "session": path, "harness": "codex", "project": project,
                        "ts": ts, "kind": kind, "tool": tool, "cause": cause,
                        "detail": redact(detail_raw)[:200], "signature": signature,
                    }
