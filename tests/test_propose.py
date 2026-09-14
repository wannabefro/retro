"""Tests for lib.propose: judgement briefs, the eligibility gate, and record's refusals."""
import argparse
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import ledger
from lib.propose import PROTECTED_PATTERNS, build_briefs, is_protected_path

_RETRO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "retro"
)


def _load_retro():
    """Load bin/retro as a module so cmd_record is testable directly."""
    loader = importlib.machinery.SourceFileLoader("retro_propose_cli", _RETRO_PATH)
    spec = importlib.util.spec_from_loader("retro_propose_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _record_args(**overrides):
    base = dict(
        key="tool_error:Bash:command not found", type="prose", path="rules/example.md",
        revert=None, probation_days=14, days=30, force=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def ev(session="/tmp/s1.jsonl", harness="claude", project="app",
       kind="tool_error", tool="Bash", cause="command not found", detail="d",
       signature="(none)"):
    return {
        "session": session, "harness": harness, "project": project,
        "kind": kind, "tool": tool, "cause": cause, "detail": detail,
        "signature": signature,
    }


def cluster_record(key="tool_error:Bash:command not found", count=10, per_day=1.0,
                    sessions=4, scope="global", window_days=30):
    return {
        "key": key, "kind": "tool_error", "tool": "Bash", "cause": "command not found",
        "signature": "", "count": count, "sessions": sessions, "projects": ["app"],
        "harnesses": ["claude"], "per_day": per_day, "window_days": window_days,
        "scope": scope,
    }


class BuildBriefsLimitOrderingTests(unittest.TestCase):
    def test_returns_at_most_limit_briefs_highest_count_first(self):
        clusters = [
            cluster_record(key="a", count=5),
            cluster_record(key="b", count=50),
            cluster_record(key="c", count=20),
        ]
        events = [ev(kind="tool_error", tool="Bash", cause="command not found")]
        briefs, skipped = build_briefs(events, clusters, {"rules": []}, limit=2)
        self.assertEqual(len(briefs), 2)
        self.assertEqual([b["cluster"]["key"] for b in briefs], ["b", "c"])
        self.assertEqual(skipped, [])


class EligibilitySkipReasonTests(unittest.TestCase):
    def test_skips_a_key_already_on_probation(self):
        clusters = [cluster_record(key="a")]
        ledger_data = {"rules": [{"cluster_key": "a", "status": "probation"}]}
        briefs, skipped = build_briefs([], clusters, ledger_data, limit=5)
        self.assertEqual(briefs, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["key"], "a")
        self.assertIn("probation", skipped[0]["reason"])

    def test_skips_a_key_previously_reverted(self):
        clusters = [cluster_record(key="a")]
        ledger_data = {"rules": [{"cluster_key": "a", "status": "reverted"}]}
        briefs, skipped = build_briefs([], clusters, ledger_data, limit=5)
        self.assertEqual(briefs, [])
        self.assertIn("reverted", skipped[0]["reason"])

    def test_skips_a_cluster_whose_scope_does_not_match_requested_scope(self):
        clusters = [cluster_record(key="a", scope="project:app")]
        briefs, skipped = build_briefs([], clusters, {"rules": []}, limit=5, scope="global")
        self.assertEqual(briefs, [])
        self.assertIn("scope", skipped[0]["reason"])


class SampleCappingTests(unittest.TestCase):
    def test_caps_samples_at_twelve_when_cluster_has_more(self):
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=20)]
        events = [ev(session=f"/tmp/s{i}.jsonl") for i in range(20)]
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        self.assertEqual(len(briefs[0]["samples"]), 12)

    def test_keeps_all_samples_when_cluster_has_fewer(self):
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=3)]
        events = [ev(session=f"/tmp/s{i}.jsonl") for i in range(3)]
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        self.assertEqual(len(briefs[0]["samples"]), 3)

    def test_samples_expose_only_the_basename_never_a_full_path(self):
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=1)]
        events = [ev(session="/Users/someone/dev/app/session-abc.jsonl")]
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        session_value = briefs[0]["samples"][0]["session"]
        self.assertNotIn("/", session_value)
        self.assertEqual(session_value, "session-abc.jsonl")


class SampleRoundRobinTests(unittest.TestCase):
    def test_a_session_burst_does_not_crowd_out_other_sessions(self):
        """A cluster spanning 4 sessions must show all 4, not just the first session's burst."""
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=20)]
        events = (
            [ev(session="/tmp/a.jsonl", detail=f"a{i}") for i in range(15)]
            + [ev(session="/tmp/b.jsonl", detail=f"b{i}") for i in range(2)]
            + [ev(session="/tmp/c.jsonl", detail=f"c{i}") for i in range(2)]
            + [ev(session="/tmp/d.jsonl", detail="d0")]
        )
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        sessions = {sample["session"] for sample in briefs[0]["samples"]}
        self.assertEqual(sessions, {"a.jsonl", "b.jsonl", "c.jsonl", "d.jsonl"})

    def test_round_robin_order_matches_the_worked_example(self):
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=8)]
        events = (
            [ev(session="/tmp/A.jsonl", detail=f"A{i}") for i in range(1, 6)]
            + [ev(session="/tmp/B.jsonl", detail=f"B{i}") for i in range(1, 3)]
            + [ev(session="/tmp/C.jsonl", detail="C1")]
        )
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        details = [sample["detail"] for sample in briefs[0]["samples"][:4]]
        self.assertEqual(details, ["A1", "B1", "C1", "A2"])

    def test_fewer_events_than_max_samples_loses_none(self):
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=3)]
        events = [ev(session=f"/tmp/s{i}.jsonl", detail=f"d{i}") for i in range(3)]
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        details = {sample["detail"] for sample in briefs[0]["samples"]}
        self.assertEqual(details, {"d0", "d1", "d2"})

    def test_one_session_only_keeps_chronological_order_unchanged(self):
        """The single-session case must not regress: order stays the raw event order."""
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=20)]
        events = [ev(session="/tmp/s1.jsonl", detail=f"d{i}") for i in range(20)]
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        details = [sample["detail"] for sample in briefs[0]["samples"]]
        self.assertEqual(details, [f"d{i}" for i in range(12)])

    def test_exactly_max_samples_events_returns_all_of_them(self):
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=12)]
        events = [ev(session="/tmp/s1.jsonl", detail=f"d{i}") for i in range(12)]
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        self.assertEqual(len(briefs[0]["samples"]), 12)

    def test_one_more_than_max_samples_still_caps_at_max_samples(self):
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=13)]
        events = [ev(session="/tmp/s1.jsonl", detail=f"d{i}") for i in range(13)]
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        self.assertEqual(len(briefs[0]["samples"]), 12)


class SignatureBreakdownTests(unittest.TestCase):
    def test_counts_two_distinct_signatures_correctly(self):
        """A coarse cause can still hide two unrelated signatures, as the cd bug did."""
        key = "tool_error:Bash:command not found"
        clusters = [cluster_record(key=key, count=5)]
        events = (
            [ev(cause="command not found", signature="sig-x") for _ in range(3)]
            + [ev(cause="command not found", signature="sig-y") for _ in range(2)]
        )
        briefs, _ = build_briefs(events, clusters, {"rules": []}, limit=5)
        self.assertEqual(briefs[0]["signatures"], {"sig-x": 3, "sig-y": 2})


class ProtectedPathTests(unittest.TestCase):
    def test_every_protected_pattern_is_refused(self):
        for name in PROTECTED_PATTERNS:
            with self.subTest(name=name):
                self.assertTrue(is_protected_path(f"/tmp/some/dir/{name}"))

    def test_a_bash_safety_shaped_path_is_refused(self):
        self.assertTrue(is_protected_path("/tmp/repo/hooks/bash-safety.sh"))

    def test_a_worktree_boundary_shaped_path_is_refused(self):
        self.assertTrue(is_protected_path("/tmp/repo/hooks/worktree-boundary.py"))

    def test_an_ordinary_path_is_not_refused(self):
        self.assertFalse(is_protected_path("/tmp/repo/rules/example.md"))


class RecordRefusalTests(unittest.TestCase):
    def _run(self, retro, tmp_path, **overrides):
        with mock.patch.object(retro.ledger, "DEFAULT_PATH", tmp_path):
            buf = io.StringIO()
            with mock.patch("sys.stderr", buf), mock.patch("sys.stdout", io.StringIO()):
                code = retro.cmd_record(_record_args(**overrides))
            return code, buf.getvalue()

    def test_no_current_cluster_exits_3(self):
        retro = _load_retro()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            with mock.patch("lib.extract.discover", return_value=[]), \
                 mock.patch("lib.extract.extract_events", return_value=[]):
                code, err = self._run(retro, tmp_path, key="no-such-key")
        self.assertEqual(code, 3)
        self.assertIn("no-such-key", err)

    def test_already_on_probation_exits_4(self):
        retro = _load_retro()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            data = {"rules": [{"id": "rule-a", "cluster_key": "dup-key", "status": "probation"}]}
            ledger.save(data, tmp_path)
            code, err = self._run(retro, tmp_path, key="dup-key")
        self.assertEqual(code, 4)
        self.assertIn("rule-a", err)

    def test_previously_reverted_exits_5(self):
        retro = _load_retro()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            data = {"rules": [{"id": "rule-b", "cluster_key": "gone-key", "status": "reverted"}]}
            ledger.save(data, tmp_path)
            code, err = self._run(retro, tmp_path, key="gone-key")
        self.assertEqual(code, 5)
        self.assertIn("rule-b", err)

    def test_protected_path_exits_6(self):
        retro = _load_retro()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            code, err = self._run(retro, tmp_path, path="/tmp/repo/settings.json")
        self.assertEqual(code, 6)
        self.assertIn("settings.json", err)

    def test_force_overrides_only_the_reverted_refusal(self):
        retro = _load_retro()
        record = cluster_record(key="gone-key", count=8, per_day=1.5, window_days=30)
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            data = {"rules": [{"id": "rule-c", "cluster_key": "gone-key", "status": "reverted"}]}
            ledger.save(data, tmp_path)
            with mock.patch("lib.extract.discover", return_value=[]), \
                 mock.patch("lib.extract.extract_events", return_value=[]), \
                 mock.patch.object(retro.cluster, "cluster_all", return_value=([record], [])):
                code, _ = self._run(retro, tmp_path, key="gone-key", force=True)
        self.assertEqual(code, 0)

    def test_force_does_not_override_the_probation_refusal(self):
        retro = _load_retro()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            data = {"rules": [{"id": "rule-d", "cluster_key": "dup-key", "status": "probation"}]}
            ledger.save(data, tmp_path)
            code, _ = self._run(retro, tmp_path, key="dup-key", force=True)
        self.assertEqual(code, 4)

    def test_force_does_not_override_the_protected_path_refusal(self):
        retro = _load_retro()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            code, _ = self._run(retro, tmp_path, path="/tmp/repo/settings.json", force=True)
        self.assertEqual(code, 6)

    def test_force_does_not_override_the_no_cluster_refusal(self):
        retro = _load_retro()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            with mock.patch("lib.extract.discover", return_value=[]), \
                 mock.patch("lib.extract.extract_events", return_value=[]):
                code, _ = self._run(retro, tmp_path, key="no-such-key", force=True)
        self.assertEqual(code, 3)


class RecordSuccessTests(unittest.TestCase):
    def test_success_writes_a_ledger_entry_with_a_measure_version(self):
        retro = _load_retro()
        record = cluster_record(key="tool_error:Bash:command not found", count=12,
                                 per_day=2.0, window_days=30)
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "ledger.json")
            with mock.patch.object(retro.ledger, "DEFAULT_PATH", tmp_path), \
                 mock.patch("lib.extract.discover", return_value=[]), \
                 mock.patch("lib.extract.extract_events", return_value=[]), \
                 mock.patch.object(retro.cluster, "cluster_all", return_value=([record], [])), \
                 mock.patch("sys.stdout", io.StringIO()):
                code = retro.cmd_record(_record_args())
            saved = ledger.load(tmp_path)
        self.assertEqual(code, 0)
        self.assertEqual(len(saved["rules"]), 1)
        entry = saved["rules"][0]
        self.assertTrue(entry["baseline"]["measure_version"])
        self.assertEqual(entry["status"], "probation")


if __name__ == "__main__":
    unittest.main()
