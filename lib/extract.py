"""Turn coding-agent session transcripts into a stream of typed failure events.

Kinds: tool_error, retry_identical, retry_after_error, compaction, user_interrupt, permission_denied.
"""
import os
import re
import subprocess
import time
from pathlib import Path

from lib.adapters import claude_code, codex, cursor

ADAPTERS = (claude_code, codex, cursor)

INJECTION_MARKERS = (
    "<task-notification>",
    "<system-reminder>",
    "<local-command-stdout>",
    "<command-name>",
    "Base directory for this skill:",
    "Caveat: The messages below",
    "This session is being continued",
)


def is_injected(text: str) -> bool:
    """Return True when text opens with a harness-generated wrapper, not a human message."""
    head = (text or "")[:200]
    return any(marker in head for marker in INJECTION_MARKERS)


_HOME_RE = re.compile(r"(?:/Users/|/home/)[^/\s]+")
_TOKEN_RE = re.compile(r"\b(?:ghp_[A-Za-z0-9]+|sk-[A-Za-z0-9]+|xox[baprs]-[A-Za-z0-9-]+|AKIA[A-Z0-9]{12,})\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_GENERIC_SECRET_RE = re.compile(r"\b(?=[A-Za-z0-9_\-]{32,}\b)(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{32,}\b")
_PKG_SCOPE_RE = re.compile(r"@[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_HOSTNAME_SUFFIX_RE = re.compile(rf"\b(?:{_LABEL}\.)+(?:internal|corp|local|lan)\b", re.IGNORECASE)
_HOSTNAME_DEV_IO_RE = re.compile(rf"\b{_LABEL}\.({_LABEL})\.(?:dev|io)\b", re.IGNORECASE)

_denylist_state = None  # (terms, compiled_pattern_or_None), populated lazily by load_denylist


def _compile_denylist_pattern(terms):
    """Compile denylist terms as one case-insensitive substring matcher, never word-bounded."""
    if not terms:
        return None
    return re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)


def load_denylist(path=None):
    """Load redact terms from path, default ~/.retro/redact.txt, caching module-wide."""
    global _denylist_state
    if path is not None:
        _denylist_state = _read_denylist(Path(path))
    elif _denylist_state is None:
        _denylist_state = _read_denylist(Path.home() / ".retro" / "redact.txt")
    return _denylist_state[0]


def _read_denylist(path):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    terms = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    return terms, _compile_denylist_pattern(terms)


def _redact_hostnames(text, terms):
    result = _HOSTNAME_SUFFIX_RE.sub("<host>", text)
    if not terms:
        return result
    lowered = {t.lower() for t in terms}
    return _HOSTNAME_DEV_IO_RE.sub(lambda m: "<host>" if m.group(1).lower() in lowered else m.group(0), result)


def redact(text: str) -> str:
    """Strip home paths, tokens, emails, package scopes, hostnames, and denylist terms before text becomes public."""
    if not text:
        return text
    result = _HOME_RE.sub("~", text)
    result = _TOKEN_RE.sub("<redacted>", result)
    result = _EMAIL_RE.sub("<email>", result)
    result = _GENERIC_SECRET_RE.sub("<redacted>", result)
    result = _PKG_SCOPE_RE.sub("<pkg>", result)
    terms = load_denylist()
    result = _redact_hostnames(result, terms)
    _, pattern = _denylist_state
    if pattern:
        result = pattern.sub("<redacted>", result)
    return result


_LEAK_CHECKS = (
    ("home_path", _HOME_RE),
    ("email", _EMAIL_RE),
    ("token", _TOKEN_RE),
    ("token", _GENERIC_SECRET_RE),
    ("package_scope", _PKG_SCOPE_RE),
)


def scan_for_leaks(texts, extra_terms=()):
    """Return (index, matched_pattern) for each text still carrying a leak after redaction."""
    terms = list(load_denylist()) + list(extra_terms)
    denylist_pattern = _compile_denylist_pattern(terms)
    findings = []
    for i, text in enumerate(texts):
        t = text or ""
        matched = next((name for name, pat in _LEAK_CHECKS if pat.search(t)), None)
        if matched is None and denylist_pattern and denylist_pattern.search(t):
            matched = "denylist_term"
        if matched:
            findings.append((i, matched))
    return findings


def classify_cause(text: str) -> str:
    """Map raw error text to the closed cause vocabulary, defaulting to other."""
    t = (text or "").lower()
    if "hook" in t and ("block" in t or "denied" in t or "non-zero exit" in t):
        return "hook blocked"
    if "permission" in t and "denied" in t and "user" in t:
        return "permission denied by user"
    if "user rejected" in t or "user declined" in t or "doesn't want to proceed" in t:
        return "permission denied by user"
    if "not been read" in t or "read the file before" in t or "must read the file" in t:
        return "needs prior read"
    if "not unique" in t or "multiple matches" in t or "appears multiple times" in t:
        return "string not unique"
    if "string to replace not found" in t or "string not found" in t or "no match found" in t or "did not match" in t:
        return "string not found"
    if "command not found" in t:
        return "command not found"
    if "no such file or directory" in t or "file not found" in t or "enoent" in t or "does not exist" in t:
        return "file not found"
    if "timed out" in t or "timeout" in t:
        return "timeout"
    if "too many open files" in t or "emfile" in t:
        return "too many open files"
    if "merge conflict" in t or "conflict marker" in t:
        return "git conflict"
    if "test failed" in t or "tests failed" in t or "build failed" in t or "compilation failed" in t or "assertionerror" in t:
        return "test or build failure"
    return "other"


SHELL_TOOLS = frozenset({"Bash", "exec", "exec_command", "CommandExecution"})
_EXPECTED_EXIT_BINARIES = frozenset({"grep", "rg", "ag", "ack", "diff", "cmp", "fgrep", "egrep"})
_WRAPPER_WORDS = frozenset({"sudo", "env", "command", "nohup", "time", "exec"})
_SETUP_VERBS = frozenset({"cd", "export", "source", ".", "pushd"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CLAUSE_SPLIT_RE = re.compile(r"&&|\|\||;")

_QUOTED_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
_PATH_RE = re.compile(r'(?:~|\.{1,2})?(?:/[\w.-]+)+|[\w.-]+(?:/[\w.-]+)+')
_DIGITS_RE = re.compile(r'\d+')
_EXIT_CODE_RE = re.compile(r"Exit code (\d+)")


def _strip_wrapper_words(tokens):
    i = 0
    while i < len(tokens) and tokens[i] in _WRAPPER_WORDS:
        i += 1
    return tokens[i:]


def command_head(command: str) -> str:
    """Return the real first command word, skipping leading cd/export/source/pushd setup clauses."""
    last_setup_head = ""
    for clause in _CLAUSE_SPLIT_RE.split(command or ""):
        tokens = clause.split()
        while tokens and _ASSIGNMENT_RE.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue
        if tokens[0] in _SETUP_VERBS:
            last_setup_head = tokens[0]
            continue
        tokens = _strip_wrapper_words(tokens)
        if tokens:
            return tokens[0]
    return last_setup_head


def binary_of(command_line: str) -> str:
    """Return the shell command's real first token, skipping setup clauses and wrappers."""
    return command_head(command_line)


def exit_code_from_text(text: str):
    """Parse the leading Exit code N marker Claude Code writes into a Bash result. None if absent."""
    m = _EXIT_CODE_RE.match((text or "").lstrip())
    return int(m.group(1)) if m else None


def signature_of(text: str) -> str:
    """Normalize the first meaningful line of error text into a short redacted fingerprint."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped in ("{", "["):
            continue
        normalized = _QUOTED_RE.sub("S", stripped)
        normalized = _PATH_RE.sub("P", normalized)
        normalized = _DIGITS_RE.sub("N", normalized)
        result = redact(normalized)[:60].strip()
        return result if result else "(none)"
    return "(none)"


def classify_and_sign(tool: str, detail: str, exit_code=None, binary: str = ""):
    """Classify a tool result and build its signature: exit 1 from a search/diff binary is expected."""
    if tool in SHELL_TOOLS and exit_code is not None:
        if binary in _EXPECTED_EXIT_BINARIES and exit_code == 1:
            cause = "expected exit"
        else:
            cause = classify_cause(detail)
        signature = redact(f"{binary}:{exit_code}")[:60].strip() or "(none)"
        return cause, signature
    return classify_cause(detail), signature_of(detail)


_GENERIC_SLUG_WORDS = frozenset({"worktrees", "sessions", "projects", "tmp", "var", "private", "dev", "home"})


def _project_from_slug(slug: str) -> str:
    """Guess a repository-like name from a dash-slugified transcript directory name."""
    parts = slug.split("-")
    idx = len(parts) - 1
    while idx >= 0:
        token = parts[idx]
        if token and not token.isdigit() and token.lower() not in _GENERIC_SLUG_WORDS:
            break
        idx -= 1
    if idx < 0:
        return ""
    name = parts[idx]
    if idx > 0 and parts[idx - 1] == "":
        name = "." + name
    return name


_PROJECT_CACHE = {}


def _resolve_repo_name(cwd):
    basename = os.path.basename(cwd.rstrip("/"))
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return basename
    if result.returncode != 0 or not result.stdout.strip():
        return basename
    common_dir = result.stdout.strip()
    if not os.path.isabs(common_dir):
        common_dir = os.path.join(cwd, common_dir)
    return os.path.basename(os.path.dirname(os.path.normpath(common_dir))) or basename


def project_for_cwd(cwd: str) -> str:
    """Resolve cwd's git repository name via --git-common-dir, caching per cwd, capped at 40 chars."""
    if not cwd:
        return ""
    if cwd not in _PROJECT_CACHE:
        _PROJECT_CACHE[cwd] = _resolve_repo_name(cwd)[:40]
    return _PROJECT_CACHE[cwd]


def reset_project_cache():
    """Clear the module-level cwd-to-project cache. Tests use this between cases."""
    _PROJECT_CACHE.clear()


def resolve_project(cwd: str, slug_fallback: str = "") -> str:
    """Resolve a short project label: cwd's git repo name, else a repo-like slug segment, else empty."""
    if cwd:
        name = project_for_cwd(cwd)
        if name:
            return name
    if slug_fallback:
        name = _project_from_slug(slug_fallback)
        if name:
            return name[:40]
    return ""


def _first_lines(path, limit=60):
    lines = []
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                lines.append(line)
    except OSError:
        pass
    return lines


def extract_events(paths, since_days=30):
    """Detect each transcript's harness and yield its events, cut off since_days back."""
    since_ts = time.time() - since_days * 86400
    for path in paths:
        first_lines = _first_lines(path)
        if not first_lines:
            continue
        for adapter in ADAPTERS:
            if adapter.detect(first_lines):
                yield from adapter.events(path, since_ts)
                break


def discover(roots=None, since_days=30):
    """List transcript paths newer than since_days across the three harnesses."""
    if roots is None:
        home = Path.home()
        roots = [
            home / ".claude" / "projects",
            home / ".codex" / "sessions",
            home / ".agents" / "sessions",
            home / ".cursor" / "projects",
        ]
    cutoff = time.time() - since_days * 86400
    found = []
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for p in root_path.rglob("*.jsonl"):
            try:
                if p.stat().st_mtime >= cutoff:
                    found.append(str(p))
            except OSError:
                continue
    return sorted(set(found))
