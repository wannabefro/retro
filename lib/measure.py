"""Fingerprint the measurement instrument, so a verdict is invalid across a change to it."""
import ast
import hashlib
import inspect
import re

try:
    import lib.cluster as cluster
    import lib.extract as extract
except ImportError as exc:
    raise RuntimeError(f"measure_version: could not import a sibling module: {exc}") from exc

_KINDS_RE = re.compile(r"Kinds:\s*(.+?)\.")


def _cause_vocabulary():
    """Read the closed cause vocabulary from classify_cause's return string literals."""
    source = inspect.getsource(extract.classify_cause)
    tree = ast.parse(source)
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _kind_vocabulary():
    """Read the closed event-kind vocabulary from lib.extract's module docstring."""
    match = _KINDS_RE.search(extract.__doc__ or "")
    if not match:
        raise RuntimeError("lib.extract module docstring has no 'Kinds:' line to fingerprint")
    return {name.strip() for name in match.group(1).split(",")}


def _normalize_source(source):
    """Drop blank lines, full-line comments, and edge whitespace, so formatting cannot churn the hash."""
    lines = (line.strip() for line in source.splitlines())
    return "\n".join(line for line in lines if line and not line.startswith("#"))


def fingerprint(causes, noise_causes, noise_kinds, uninformative_causes, kinds, key_source):
    """Hash the sorted fingerprint inputs into a stable 12-character hex digest."""
    parts = [
        ",".join(sorted(causes)),
        ",".join(sorted(noise_causes)),
        ",".join(sorted(noise_kinds)),
        ",".join(sorted(uninformative_causes)),
        ",".join(sorted(kinds)),
        _normalize_source(key_source),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def measure_version():
    """Fingerprint the current cause vocabulary, noise sets, kind vocabulary, and cluster_key logic."""
    try:
        causes = _cause_vocabulary()
        noise_causes = cluster.NOISE_CAUSES
        noise_kinds = cluster.NOISE_KINDS
        uninformative_causes = cluster.UNINFORMATIVE_CAUSES
        kinds = _kind_vocabulary()
        key_source = inspect.getsource(cluster.cluster_key)
    except AttributeError as exc:
        raise RuntimeError(f"measure_version: a sibling module is missing an expected attribute: {exc}") from exc
    return fingerprint(causes, noise_causes, noise_kinds, uninformative_causes, kinds, key_source)
