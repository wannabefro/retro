"""Claude Code session JSONL: message.role blocks, tool_use/tool_result correlation."""
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
        msg = obj.get("message")
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
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


def _block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts)
    return ""


def _find_cwd(path, limit=20):
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


def events(path, since_ts):
    from lib.extract import SHELL_TOOLS, binary_of, classify_and_sign, exit_code_from_text, redact, resolve_project

    path = os.path.abspath(path)
    slug = os.path.basename(os.path.dirname(path))
    project = resolve_project(_find_cwd(path), slug)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0

    tool_calls = {}  # tool_use id -> (name, input dict)
    call_error = {}  # tool_use id -> bool, did that call's result come back an error
    prev_call = None
    prev_call_id = None

    with open(path, "r", errors="replace") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue

            ts = _parse_ts(obj, mtime)
            msg = obj.get("message")

            if obj.get("isCompactSummary"):
                text = ""
                if isinstance(msg, dict):
                    content = msg.get("content")
                    text = _block_text(content) if isinstance(content, list) else (content if isinstance(content, str) else "")
                if ts >= since_ts:
                    yield {
                        "session": path, "harness": "claude", "project": project,
                        "ts": ts, "kind": "compaction", "tool": "", "cause": "other",
                        "detail": redact(text[:200]), "signature": "(none)",
                    }

            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")

            if role == "user":
                text = _block_text(content) if isinstance(content, list) else (content if isinstance(content, str) else "")
                if "[Request interrupted by user" in text and ts >= since_ts:
                    yield {
                        "session": path, "harness": "claude", "project": project,
                        "ts": ts, "kind": "user_interrupt", "tool": "", "cause": "other",
                        "detail": redact(text[:200]), "signature": "(none)",
                    }
                denial_kind = obj.get("toolDenialKind")
                if isinstance(content, list):
                    for block in content:
                        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                            continue
                        tool_use_id = block.get("tool_use_id")
                        tool_name, tool_input = tool_calls.get(tool_use_id, ("", {}))
                        detail_raw = _block_text(block.get("content"))
                        is_error = bool(block.get("is_error"))
                        call_error[tool_use_id] = bool(denial_kind) or is_error
                        if denial_kind:
                            cause = "permission denied by user"
                            kind = "permission_denied"
                            signature = redact(detail_raw)[:60].strip() or "(none)"
                        elif is_error:
                            is_shell = tool_name in SHELL_TOOLS
                            command = tool_input.get("command", "") if is_shell else ""
                            binary = binary_of(command) if command else ""
                            exit_code = exit_code_from_text(detail_raw) if is_shell else None
                            cause, signature = classify_and_sign(tool_name, detail_raw, exit_code=exit_code, binary=binary)
                            kind = "permission_denied" if cause == "permission denied by user" else "tool_error"
                        else:
                            continue
                        if ts >= since_ts:
                            yield {
                                "session": path, "harness": "claude", "project": project,
                                "ts": ts, "kind": kind, "tool": tool_name, "cause": cause,
                                "detail": redact(detail_raw)[:200], "signature": signature,
                            }
                continue

            if role == "assistant" and isinstance(content, list):
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    tool_id = block.get("id")
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    if tool_id:
                        tool_calls[tool_id] = (tool_name, tool_input)
                    serialized = json.dumps(tool_input, sort_keys=True)[:500]
                    current_call = (tool_name, serialized)
                    if prev_call is not None and current_call == prev_call and ts >= since_ts:
                        prev_errored = call_error.get(prev_call_id, False)
                        kind = "retry_after_error" if prev_errored else "retry_identical"
                        yield {
                            "session": path, "harness": "claude", "project": project,
                            "ts": ts, "kind": kind, "tool": tool_name,
                            "cause": "other", "detail": redact(serialized)[:200],
                            "signature": "(none)",
                        }
                    prev_call = current_call
                    prev_call_id = tool_id
