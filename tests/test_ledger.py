"""Tests for lib.ledger and lib.verify — the probation store and its verdict."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import ledger
from lib import verify
from lib.measure import measure_version


def _cluster(key="tool_error:Bash:command not found", count=10, per_day=2.5,
             window_days=14, scope="global"):
    return {"key": key, "count": count, "per_day": per_day,
            "window_days": window_days, "scope": scope}


def _artifact(path="rules/example.md"):
    return {"type": "prose", "path": path, "revert": ""}


class LoadTests(unittest.TestCase):
    def test_missing_path_returns_empty_shape(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "no-such-ledger.json")
            self.assertEqual(ledger.load(missing), {"rules": []})

    def test_corrupt_json_returns_empty_shape(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.json")
            with open(path, "w") as f:
                f.write("{not json")
            self.assertEqual(ledger.load(path), {"rules": []})


class SaveLoadRoundTripTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.json")
            data = {"rules": [{"id": "x", "status": "probation"}]}
            ledger.save(data, path)
            self.assertEqual(ledger.load(path), data)

    def test_temp_file_shares_target_directory(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.json")
            with mock.patch.object(ledger.tempfile, "mkstemp",
                                    wraps=ledger.tempfile.mkstemp) as spy:
                ledger.save({"rules": []}, path)
                _, kwargs = spy.call_args
                self.assertEqual(kwargs["dir"], d)

    def test_save_is_atomic_old_content_survives_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.json")
            original = {"rules": [{"id": "kept-one", "status": "kept"}]}
            ledger.save(original, path)

            with mock.patch.object(ledger.os, "replace",
                                    side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    ledger.save({"rules": [{"id": "new", "status": "probation"}]}, path)

            self.assertEqual(ledger.load(path), original)
            leftovers = [n for n in os.listdir(d) if n != "ledger.json"]
            self.assertEqual(leftovers, [])


class AddIdSlugTests(unittest.TestCase):
    def test_slug_from_realistic_cluster_key(self):
        data = {"rules": []}
        entry = ledger.add(data, _cluster(key="tool_error:Bash:command not found"),
                            _artifact())
        self.assertEqual(entry["id"], "tool-error-bash-command-not-found")

    def test_duplicate_id_gets_numbered_suffix(self):
        data = {"rules": []}
        first = ledger.add(data, _cluster(key="tool_error:Bash:x"), _artifact())
        second = ledger.add(data, _cluster(key="tool_error:Bash:x"), _artifact())
        third = ledger.add(data, _cluster(key="tool_error:Bash:x"), _artifact())
        self.assertEqual(first["id"], "tool-error-bash-x")
        self.assertEqual(second["id"], "tool-error-bash-x-2")
        self.assertEqual(third["id"], "tool-error-bash-x-3")

    def test_entry_shape_matches_frozen_contract(self):
        data = {"rules": []}
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        entry = ledger.add(data, _cluster(), _artifact(), probation_days=14, now=now)
        self.assertEqual(entry["written_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(entry["checkpoint_at"], "2026-01-15T00:00:00Z")
        self.assertEqual(entry["status"], "probation")
        self.assertEqual(entry["result"], {})
        self.assertEqual(entry["baseline"],
                          {"window_days": 14, "count": 10, "per_day": 2.5,
                           "measure_version": measure_version()})


class GetTests(unittest.TestCase):
    def test_get_found_and_missing(self):
        data = {"rules": []}
        entry = ledger.add(data, _cluster(), _artifact())
        self.assertEqual(ledger.get(data, entry["id"]), entry)
        self.assertIsNone(ledger.get(data, "no-such-id"))


class DueTests(unittest.TestCase):
    def test_due_selects_only_probation_past_checkpoint(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        past = now - timedelta(days=1)
        future = now + timedelta(days=1)
        data = {"rules": [
            {"id": "due-and-probation", "status": "probation",
             "checkpoint_at": past.strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"id": "not-due-yet", "status": "probation",
             "checkpoint_at": future.strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"id": "past-but-kept", "status": "kept",
             "checkpoint_at": past.strftime("%Y-%m-%dT%H:%M:%SZ")},
        ]}
        result = ledger.due(data, now=now)
        self.assertEqual([r["id"] for r in result], ["due-and-probation"])


class VerdictBoundaryTests(unittest.TestCase):
    def test_delta_exactly_at_threshold_is_kept(self):
        entry = {"baseline": {"per_day": 100.0}}
        v = verify.verdict(entry, {"per_day": 75.0})
        self.assertEqual(v["delta_pct"], -0.25)
        self.assertEqual(v["status"], "kept")

    def test_delta_just_above_threshold_is_reverted(self):
        entry = {"baseline": {"per_day": 100.0}}
        v = verify.verdict(entry, {"per_day": 75.1})
        self.assertEqual(v["delta_pct"], -0.249)
        self.assertEqual(v["status"], "reverted")


class VerdictZeroEdgeCaseTests(unittest.TestCase):
    def test_zero_baseline_is_reverted(self):
        entry = {"baseline": {"per_day": 0}}
        v = verify.verdict(entry, {"per_day": 5.0})
        self.assertEqual(v["status"], "reverted")
        self.assertEqual(v["delta_pct"], 0.0)
        self.assertIn("baseline", v["reason"].lower())

    def test_zero_current_is_kept_with_full_drop(self):
        entry = {"baseline": {"per_day": 10.0}}
        v = verify.verdict(entry, {"per_day": 0})
        self.assertEqual(v["status"], "kept")
        self.assertEqual(v["delta_pct"], -1.0)


class ApplyVerdictTests(unittest.TestCase):
    def test_mutates_status_and_result_without_touching_disk(self):
        data = {"rules": []}
        entry = ledger.add(data, _cluster(per_day=10.0), _artifact())
        with mock.patch.object(ledger, "save", side_effect=AssertionError("must not save")):
            updated = verify.apply_verdict(data, entry["id"], {"per_day": 1.0})
        self.assertEqual(updated["status"], "kept")
        self.assertEqual(updated["result"]["status"], "kept")
        self.assertIs(ledger.get(data, entry["id"]), updated)

    def test_unknown_rule_id_raises(self):
        data = {"rules": []}
        with self.assertRaises(ValueError):
            verify.apply_verdict(data, "missing", {"per_day": 1.0})


class AddStampsMeasureVersionTests(unittest.TestCase):
    def test_add_stamps_current_measure_version(self):
        data = {"rules": []}
        entry = ledger.add(data, _cluster(), _artifact())
        self.assertEqual(entry["baseline"]["measure_version"], measure_version())


class VerdictMeasureVersionTests(unittest.TestCase):
    def test_matching_version_matches_no_version_arg(self):
        entry = {"baseline": {"per_day": 4.27, "measure_version": "abc123"}}
        current = {"per_day": 0.2}
        self.assertEqual(verify.verdict(entry, current, "abc123"), verify.verdict(entry, current))

    def test_differing_version_is_unmeasurable_even_though_numbers_say_kept(self):
        entry = {"baseline": {"per_day": 4.27, "measure_version": "old-version"}}
        current = {"per_day": 0.2}
        self.assertEqual(verify.verdict(entry, current)["status"], "kept")
        result = verify.verdict(entry, current, "new-version")
        self.assertEqual(result["status"], "unmeasurable")
        self.assertIsNone(result["delta_pct"])

    def test_missing_measure_version_is_treated_as_differing(self):
        entry = {"baseline": {"per_day": 4.27}}
        result = verify.verdict(entry, {"per_day": 0.2}, "new-version")
        self.assertEqual(result["status"], "unmeasurable")


class ApplyVerdictUnmeasurableTests(unittest.TestCase):
    def test_status_stays_on_probation_and_rebaseline_is_flagged(self):
        data = {"rules": []}
        entry = ledger.add(data, _cluster(per_day=4.27), _artifact())
        entry["baseline"]["measure_version"] = "old-version"
        updated = verify.apply_verdict(
            data, entry["id"], {"per_day": 0.2}, current_version="new-version")
        self.assertEqual(updated["status"], "probation")
        self.assertEqual(updated["result"]["status"], "unmeasurable")
        self.assertTrue(updated["result"]["rebaseline"])


if __name__ == "__main__":
    unittest.main()
