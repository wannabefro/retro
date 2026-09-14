"""Tests for lib.report: the window guard, the three formatters, and bin/retro's verdict helpers."""
import argparse
import importlib.machinery
import importlib.util
import io
import os
import unittest
from unittest import mock

from lib.report import assert_window, format_clusters, format_ledger, format_verdicts

_RETRO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "retro"
)


def _load_retro():
    """Load bin/retro as a module so its verdict helpers are testable directly."""
    loader = importlib.machinery.SourceFileLoader("retro_cli", _RETRO_PATH)
    spec = importlib.util.spec_from_loader("retro_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _cluster(key="tool_error:Bash:command not found", count=9, per_day=0.3,
             sessions=4, scope="global", signature=""):
    return {"key": key, "count": count, "per_day": per_day, "sessions": sessions,
            "scope": scope, "signature": signature}


def _ledger_entry(entry_id="rule-a", status="probation", per_day=2.5,
                   scope="global", checkpoint="2026-06-01T00:00:00Z", result=None):
    return {
        "id": entry_id, "scope": scope, "status": status,
        "baseline": {"window_days": 14, "count": 35, "per_day": per_day},
        "checkpoint_at": checkpoint, "result": result or {},
    }


def _verdict_row(row_id, status, revert_hint=""):
    return {
        "id": row_id,
        "verdict": {
            "status": status, "delta_pct": -0.3 if status == "kept" else 0.1,
            "baseline_per_day": 2.5, "current_per_day": 1.7,
        },
        "revert_hint": revert_hint,
    }


def _assert_lines_fit(test, text):
    for line in text.splitlines():
        test.assertLessEqual(len(line), 100)


class AssertWindowTests(unittest.TestCase):
    def test_equal_windows_pass(self):
        assert_window(30, 30)

    def test_unequal_windows_raise_naming_both_numbers(self):
        with self.assertRaises(ValueError) as ctx:
            assert_window(30, 14)
        message = str(ctx.exception)
        self.assertIn("30", message)
        self.assertIn("14", message)


class FormatClustersTests(unittest.TestCase):
    def test_empty_list_reads_as_a_sentence_not_a_blank_string(self):
        text = format_clusters([])
        self.assertTrue(text.strip())
        self.assertIn("No clusters", text)

    def test_two_row_table_aligns_numeric_columns(self):
        clusters = [
            _cluster(key="tool_error:Bash:command not found", count=40, per_day=1.33,
                     sessions=9, scope="global"),
            _cluster(key="retry_identical:Read:file not found", count=5, per_day=0.17,
                     sessions=3, scope="project:app"),
        ]
        text = format_clusters(clusters)
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        header, row1, row2 = lines
        self.assertTrue(header.startswith("COUNT"))
        self.assertEqual(row1[:5], "   40")
        self.assertEqual(row2[:5], "    5")
        _assert_lines_fit(self, text)

    def test_rejected_section_appears_under_its_own_heading(self):
        passed = [_cluster()]
        rejected = [_cluster(key="rare:Bash:noise", count=2, sessions=1)]
        text = format_clusters(passed, rejected)
        self.assertIn("Rejected (below threshold):", text)
        self.assertIn("rare:Bash:noise", text)

    def test_rejected_none_omits_the_heading(self):
        text = format_clusters([_cluster()], rejected=None)
        self.assertNotIn("Rejected", text)

    def test_a_very_long_scope_value_still_fits_the_line_budget(self):
        long_scope = "project:" + "x" * 90
        clusters = [_cluster(scope=long_scope), _cluster(key="short:key", count=6)]
        _assert_lines_fit(self, format_clusters(clusters))

    def test_signature_column_is_never_rendered(self):
        clusters = [
            _cluster(key="tool_error:Bash:other:git:128", signature="git:128"),
            _cluster(key="short:key", count=6),
        ]
        text = format_clusters(clusters)
        self.assertNotIn("SIGNATURE", text)

    def test_scope_hard_cap_frees_key_room_even_under_single_row_pressure(self):
        """A hard SCOPE cap holds with one row too, where the outlier cap cannot act."""
        clusters = [_cluster(key="a" * 200, count=1, per_day=0.1, sessions=1,
                              scope="project:" + "x" * 90)]
        row = format_clusters(clusters).splitlines()[1]
        parts = row.split()
        self.assertLessEqual(len(parts[3]), 22)
        self.assertGreater(len(parts[-1]), 40)
        self.assertIn("\u2026", parts[-1])
        self.assertLessEqual(len(row), 100)

    def test_a_large_count_is_never_truncated(self):
        clusters = [_cluster(key="short:key", count=123456789)]
        row = format_clusters(clusters).splitlines()[1]
        self.assertEqual(row.split()[0], "123456789")

    def test_long_key_row_leaves_every_other_column_intact(self):
        long_key = "permission_denied:Bash:permission denied by user:" + "z" * 65
        self.assertEqual(len(long_key), 114)
        clusters = [
            _cluster(key=long_key, count=134, per_day=19.70, sessions=57, scope="global"),
            _cluster(key="tool_error:Bash:other:cd:1", count=103, per_day=14.20,
                      sessions=50, scope="global"),
            _cluster(key="tool_error:McpToolCall:timeout", count=93, per_day=13.10,
                      sessions=4, scope="global"),
        ]
        lines = format_clusters(clusters).splitlines()
        self.assertEqual(len(lines), 4)
        _, row1, row2, row3 = lines
        self.assertIn("…", row1)
        self.assertNotIn(long_key, row1)
        self.assertEqual(row1.split()[:4], ["134", "19.70", "57", "global"])
        self.assertEqual(row2.split(), ["103", "14.20", "50", "global", "tool_error:Bash:other:cd:1"])
        self.assertEqual(row3.split(), ["93", "13.10", "4", "global", "tool_error:McpToolCall:timeout"])
        _assert_lines_fit(self, "\n".join(lines))


class OutlierWidthTests(unittest.TestCase):
    def test_outlier_scope_does_not_truncate_the_other_rows_key(self):
        long_scope = "project:" + "x" * 72
        untouched_key = "permission_denied:Bash:permission denied by user"
        clusters = [
            _cluster(scope=long_scope, key="compaction::other", count=430, sessions=249),
            _cluster(scope="global", key=untouched_key, count=233, sessions=77),
        ]
        text = format_clusters(clusters)
        self.assertIn(untouched_key, text)
        _assert_lines_fit(self, text)

    def test_middle_truncation_keeps_both_ends_of_a_long_key(self):
        long_key = "HEADHEADHEAD:" + ("m" * 180) + ":TAILTAILTAIL"
        clusters = [
            _cluster(scope="global", key=long_key, count=10, sessions=5),
            _cluster(scope="project:app", key="short:key", count=9, sessions=4),
        ]
        text = format_clusters(clusters)
        _assert_lines_fit(self, text)
        key_line = text.splitlines()[1]
        self.assertIn("HEADHEADHEAD:", key_line)
        self.assertIn(":TAILTAILTAIL", key_line)
        self.assertIn("…", key_line)

    def test_two_rows_tied_at_max_scope_length_are_still_capped(self):
        """A length tie defeats the outlier cap; the hard SCOPE maximum still holds."""
        tied_scope = "project:" + "x" * 34
        self.assertEqual(len(tied_scope), 42)
        long_key = "permission_denied:Bash:permission denied by user:" + "z" * 30
        clusters = [
            _cluster(scope=tied_scope, key=long_key, count=134, per_day=19.14, sessions=57),
            _cluster(scope=tied_scope, key="tool_error:McpToolCall:timeout", count=93,
                      per_day=13.29, sessions=4),
        ]
        text = format_clusters(clusters)
        row1 = text.splitlines()[1]
        parts = row1.split()
        self.assertLessEqual(len(parts[3]), 22)
        self.assertGreater(len(parts[-1]), 40)
        _assert_lines_fit(self, text)


class HeaderIntegrityTests(unittest.TestCase):
    """A header must always render in full, never chopped to make room for data."""

    def test_cluster_header_renders_in_full_when_key_is_long(self):
        long_key = "permission_denied:Bash:permission denied by user:" + "z" * 65
        clusters = [
            _cluster(key=long_key, count=134, per_day=19.70, sessions=57, scope="global"),
            _cluster(key="tool_error:Bash:other:cd:1", count=103, per_day=14.20,
                      sessions=50, scope="global"),
        ]
        header = format_clusters(clusters).splitlines()[0]
        self.assertEqual(header.split(), ["COUNT", "PER-DAY", "SESSIONS", "SCOPE", "KEY"])

    def test_ledger_header_renders_in_full_under_pressure(self):
        entries = [_ledger_entry(entry_id="rule-" + "a" * 30, scope="project:" + "b" * 30)]
        header = format_ledger(entries).splitlines()[0]
        self.assertEqual(
            header.split(),
            ["ID", "SCOPE", "STATUS", "BASELINE/DAY", "CHECKPOINT", "VERDICT"],
        )

    def test_verdict_header_renders_in_full_under_pressure(self):
        rows = [_verdict_row("rule-" + "a" * 40, "kept")]
        header = format_verdicts(rows).splitlines()[0]
        self.assertEqual(
            header.split(), ["ID", "STATUS", "DELTA", "BASELINE/DAY", "CURRENT/DAY"]
        )


class FormatLedgerTests(unittest.TestCase):
    def test_probation_entries_come_before_kept_and_reverted(self):
        entries = [
            _ledger_entry("z-reverted", status="reverted",
                          result={"status": "reverted", "delta_pct": 0.4}),
            _ledger_entry("a-kept", status="kept",
                          result={"status": "kept", "delta_pct": -0.5}),
            _ledger_entry("m-probation", status="probation"),
        ]
        text = format_ledger(entries)
        self.assertLess(text.index("m-probation"), text.index("a-kept"))
        self.assertLess(text.index("m-probation"), text.index("z-reverted"))
        _assert_lines_fit(self, text)

    def test_empty_ledger_reads_as_a_sentence(self):
        text = format_ledger([])
        self.assertIn("No ledger entries", text)

    def test_verdict_shown_when_result_present_and_blank_when_absent(self):
        entries = [
            _ledger_entry("has-result", status="kept",
                          result={"status": "kept", "delta_pct": -0.5}),
            _ledger_entry("no-result", status="probation"),
        ]
        text = format_ledger(entries)
        self.assertIn("kept (-50.0%)", text)


class FormatVerdictsTests(unittest.TestCase):
    def test_reverted_row_carries_its_revert_hint(self):
        rows = [_verdict_row("rule-a", "reverted", revert_hint="git revert abc1234")]
        text = format_verdicts(rows)
        self.assertIn("git revert abc1234", text)
        self.assertIn("rule-a", text)

    def test_kept_row_has_no_revert_hint_line(self):
        rows = [_verdict_row("rule-b", "kept")]
        text = format_verdicts(rows)
        self.assertNotIn("git revert", text)
        self.assertNotIn("still live", text)

    def test_empty_rows_reads_as_a_sentence(self):
        text = format_verdicts([])
        self.assertIn("No entries due", text)

    def test_lines_fit_within_100_columns(self):
        rows = [
            _verdict_row("rule-a", "reverted", revert_hint="git revert " + "a" * 40),
            _verdict_row("rule-b", "kept"),
        ]
        _assert_lines_fit(self, format_verdicts(rows))

    def test_unmeasurable_row_renders_without_crashing_and_is_not_kept(self):
        rows = [{
            "id": "rule-x",
            "verdict": {
                "status": "unmeasurable", "delta_pct": None,
                "baseline_per_day": 4.0, "current_per_day": None,
                "reason": "No cluster matches key 'no-such-key'.",
            },
            "revert_hint": "",
        }]
        text = format_verdicts(rows)
        self.assertIn("unmeasurable", text)
        self.assertNotIn("kept", text)
        _assert_lines_fit(self, text)

    def test_rebaseline_row_prints_the_recovery_line(self):
        """A version-mismatch verdict tells the user to record a fresh baseline."""
        rows = [{
            "id": "rule-y",
            "verdict": {
                "status": "unmeasurable", "delta_pct": None,
                "baseline_per_day": 4.0, "current_per_day": 3.0,
                "reason": "The baseline used measure version old, the current run used new.",
            },
            "revert_hint": "",
            "rebaseline": True,
        }]
        text = format_verdicts(rows)
        self.assertIn("fresh baseline", text)
        self.assertIn("rule-y", text)

    def test_key_miss_unmeasurable_row_has_no_rebaseline_line(self):
        """A key-miss unmeasurable row is a different case; it names no fresh baseline."""
        rows = [{
            "id": "rule-z",
            "verdict": {
                "status": "unmeasurable", "delta_pct": None,
                "baseline_per_day": 4.0, "current_per_day": None,
                "reason": "No cluster matches key 'no-such-key'.",
            },
            "revert_hint": "",
        }]
        text = format_verdicts(rows)
        self.assertNotIn("fresh baseline", text)


class UnmeasurableVerdictTests(unittest.TestCase):
    def test_key_miss_returns_none_not_a_zero_rate(self):
        retro = _load_retro()
        self.assertIsNone(retro._find_current("no-such-key", [], []))

    def test_key_hit_still_returns_the_matching_record(self):
        retro = _load_retro()
        record = {"key": "found-it", "per_day": 3.5}
        self.assertEqual(retro._find_current("found-it", [record], []), record)

    def test_unmeasurable_entry_is_not_marked_kept(self):
        retro = _load_retro()
        entry = {
            "cluster_key": "no-such-key",
            "baseline": {"per_day": 4.0, "window_days": 30, "count": 120},
        }
        result = retro._unmeasurable_verdict(entry)
        self.assertEqual(result["status"], "unmeasurable")
        self.assertNotEqual(result["status"], "kept")


class CmdVerifyVersionWiringTests(unittest.TestCase):
    """cmd_verify must fingerprint the instrument once and pass it through."""

    def _entry(self):
        return {
            "id": "rule-a",
            "cluster_key": "tool_error:Bash:command not found",
            "baseline": {"window_days": 14, "per_day": 2.5, "measure_version": "old-v"},
            "artifact": {"revert": "abc1234"},
        }

    def test_verdict_and_apply_receive_the_same_version(self):
        retro = _load_retro()
        entry = self._entry()
        current_record = {"key": entry["cluster_key"], "per_day": 1.0}
        data = {"rules": [entry]}
        verdict_calls = []
        apply_calls = []

        def fake_verdict(e, c, version=None):
            verdict_calls.append(version)
            return {"status": "kept", "delta_pct": -0.6,
                     "baseline_per_day": 2.5, "current_per_day": 1.0}

        def fake_apply(d, rule_id, c, now=None, current_version=None):
            apply_calls.append(current_version)
            return entry

        with mock.patch.object(retro.ledger, "load", return_value=data), \
             mock.patch.object(retro.ledger, "due", return_value=[entry]), \
             mock.patch.object(retro.ledger, "save"), \
             mock.patch.object(retro.measure, "measure_version", return_value="fixed-version"), \
             mock.patch("lib.extract.discover", return_value=[]), \
             mock.patch("lib.extract.extract_events", return_value=[]), \
             mock.patch.object(retro.cluster, "cluster_all", return_value=([current_record], [])), \
             mock.patch.object(retro.verify, "verdict", side_effect=fake_verdict), \
             mock.patch.object(retro.verify, "apply_verdict", side_effect=fake_apply), \
             mock.patch("sys.stdout", io.StringIO()):
            retro.cmd_verify(argparse.Namespace(apply=True, json=True))

        self.assertEqual(verdict_calls, ["fixed-version"])
        self.assertEqual(apply_calls, ["fixed-version"])

    def test_version_mismatch_flags_the_row_for_rebaseline_and_never_touches_git(self):
        """The real verify.verdict must see the wired version and mark the row unmeasurable."""
        retro = _load_retro()
        entry = self._entry()
        current_record = {"key": entry["cluster_key"], "per_day": 1.0}
        data = {"rules": [entry]}

        with mock.patch.object(retro.ledger, "load", return_value=data), \
             mock.patch.object(retro.ledger, "due", return_value=[entry]), \
             mock.patch.object(retro.measure, "measure_version", return_value="new-v"), \
             mock.patch("lib.extract.discover", return_value=[]), \
             mock.patch("lib.extract.extract_events", return_value=[]), \
             mock.patch.object(retro.cluster, "cluster_all", return_value=([current_record], [])), \
             mock.patch("subprocess.run") as fake_git:
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                retro.cmd_verify(argparse.Namespace(apply=False, json=False))
            fake_git.assert_not_called()

        output = buf.getvalue()
        self.assertIn("unmeasurable", output)
        self.assertIn("fresh baseline", output)


if __name__ == "__main__":
    unittest.main()
