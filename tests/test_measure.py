"""Tests for lib.measure: the instrument fingerprint that gates a verdict's validity."""
import os
import sys
import unittest

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
            kinds={"tool_error", "compaction"},
            key_format=measure.KEY_FORMAT,
        )

    def test_same_inputs_produce_the_same_hash(self):
        args = self._base_args()
        self.assertEqual(measure.fingerprint(**args), measure.fingerprint(**args))

    def test_different_cause_vocabulary_changes_the_hash(self):
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["causes"] = {"file not found", "other", "timeout"}
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_different_noise_kinds_changes_the_hash(self):
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["noise_kinds"] = {"retry_identical"}
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_different_key_format_changes_the_hash(self):
        base = measure.fingerprint(**self._base_args())
        changed = self._base_args()
        changed["key_format"] = "kind:tool:cause"
        self.assertNotEqual(base, measure.fingerprint(**changed))

    def test_set_ordering_does_not_change_the_hash(self):
        a = measure.fingerprint(
            causes={"b", "a"}, noise_causes=set(), noise_kinds=set(),
            kinds={"z", "y"}, key_format="k",
        )
        b = measure.fingerprint(
            causes={"a", "b"}, noise_causes=set(), noise_kinds=set(),
            kinds={"y", "z"}, key_format="k",
        )
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
