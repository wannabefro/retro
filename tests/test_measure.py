"""Tests for lib.measure: the instrument fingerprint that gates a verdict's validity."""
import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import measure


class MeasureVersionStabilityTests(unittest.TestCase):
    def test_stable_across_two_calls_in_one_process(self):
        self.assertEqual(measure.measure_version(), measure.measure_version())

    def test_returns_a_string(self):
        self.assertIsInstance(measure.measure_version(), str)


class RealVocabularyExtractionTests(unittest.TestCase):
    def test_extracts_known_cause_names_from_classify_cause(self):
        causes = measure._cause_vocabulary()
        self.assertIn("file not found", causes)
        self.assertIn("other", causes)

    def test_extracts_known_kinds_from_extract_docstring(self):
        kinds = measure._kind_vocabulary()
        self.assertIn("tool_error", kinds)
        self.assertIn("compaction", kinds)


class FingerprintSensitivityTests(unittest.TestCase):
    def _base_args(self):
        return dict(
            causes={"file not found", "other"},
            noise_causes={"expected exit"},
            noise_kinds={"compaction"},
            uninformative_causes={"other"},
            kinds={"tool_error", "compaction"},
            key_source="def cluster_key(event):\n    return event['kind']\n",
        )

    def test_same_inputs_produce_the_same_hash(self):
        args = self._base_args()
        self.assertEqual(measure.fingerprint(**args), measure.fingerprint(**args))

    def test_different_cause_vocabulary_changes_the_hash(self):
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["causes"] = {"file not found", "other", "timeout"}
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_different_noise_causes_changes_the_hash(self):
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["noise_causes"] = {"expected exit", "test or build failure"}
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_different_noise_kinds_changes_the_hash(self):
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["noise_kinds"] = {"retry_identical"}
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_different_uninformative_causes_changes_the_hash(self):
        # Regression: UNINFORMATIVE_CAUSES drives the key split, so a change to it must move the hash.
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["uninformative_causes"] = {"other", "permission denied by user"}
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_different_cluster_key_source_changes_the_hash(self):
        # Regression: the guard must catch a changed cluster_key body, not just a hand-written label.
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["key_source"] = "def cluster_key(event):\n    return event['kind'] + event['tool']\n"
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_whitespace_and_comment_only_source_diff_produces_the_same_hash(self):
        a = self._base_args()
        a["key_source"] = "def cluster_key(event):\n    # a comment\n    return event['kind']\n\n"
        b = self._base_args()
        b["key_source"] = "def cluster_key(event):\n\n\n    return event['kind']   \n"
        self.assertEqual(measure.fingerprint(**a), measure.fingerprint(**b))

    def test_set_ordering_does_not_change_the_hash(self):
        a = measure.fingerprint(
            causes={"b", "a"}, noise_causes=set(), noise_kinds=set(),
            uninformative_causes={"y", "x"}, kinds={"z", "y"}, key_source="k",
        )
        b = measure.fingerprint(
            causes={"a", "b"}, noise_causes=set(), noise_kinds=set(),
            uninformative_causes={"x", "y"}, kinds={"y", "z"}, key_source="k",
        )
        self.assertEqual(a, b)


class MissingSiblingModuleTests(unittest.TestCase):
    def test_reload_raises_runtimeerror_when_a_sibling_module_cannot_be_imported(self):
        try:
            with mock.patch.dict(sys.modules, {"lib.cluster": None}):
                with self.assertRaisesRegex(RuntimeError, "could not import a sibling module"):
                    importlib.reload(measure)
        finally:
            importlib.reload(measure)


if __name__ == "__main__":
    unittest.main()
