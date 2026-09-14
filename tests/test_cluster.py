"""Tests for lib.cluster: grouping, noise suppression, cluster_key, scope, and promote."""
import unittest

from lib.cluster import cluster, cluster_all, cluster_key, promote, NOISE_CAUSES, NOISE_KINDS


def ev(session="/tmp/s1.jsonl", harness="claude", project="app", ts=1000.0,
       kind="tool_error", tool="Bash", cause="command not found", detail="d",
       signature="sig"):
    return {
        "session": session, "harness": harness, "project": project, "ts": ts,
        "kind": kind, "tool": tool, "cause": cause, "detail": detail,
        "signature": signature,
    }


class ClusterGroupingTests(unittest.TestCase):
    def test_groups_by_three_part_key(self):
        events = [
            ev(kind="tool_error", tool="Bash", cause="command not found"),
            ev(kind="tool_error", tool="Bash", cause="file not found"),
            ev(kind="retry_identical", tool="Bash", cause="command not found"),
        ]
        passed, rejected = cluster_all(events, min_count=1, min_sessions=1, drop_noise=False)
        keys = {r["key"] for r in passed + rejected}
        self.assertEqual(keys, {
            "tool_error:Bash:command not found",
            "tool_error:Bash:file not found",
            "retry_identical:Bash:command not found",
        })

    def test_count_sessions_projects_harnesses_across_multiple(self):
        events = [
            ev(session="s1", harness="claude", project="app", detail="a"),
            ev(session="s2", harness="claude", project="app", detail="b"),
            ev(session="s3", harness="codex", project="fender", detail="c"),
        ]
        passed, _ = cluster_all(events, min_count=3, min_sessions=3)
        self.assertEqual(len(passed), 1)
        record = passed[0]
        self.assertEqual(record["count"], 3)
        self.assertEqual(record["sessions"], 3)
        self.assertEqual(record["projects"], ["app", "fender"])
        self.assertEqual(record["harnesses"], ["claude", "codex"])


class ThresholdGateTests(unittest.TestCase):
    def test_min_count_gate_passes_at_exact_threshold(self):
        events = [ev(session=f"s{i}", project="app") for i in range(5)]
        passed, rejected = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(len(passed), 1)
        self.assertEqual(len(rejected), 0)

    def test_min_count_gate_rejects_one_below_threshold(self):
        events = [ev(session=f"s{i}", project="app") for i in range(4)]
        passed, rejected = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(len(passed), 0)
        self.assertEqual(len(rejected), 1)

    def test_min_sessions_gate_passes_at_exact_threshold(self):
        events = [ev(session=f"s{i}", project="app") for i in range(3)]
        passed, rejected = cluster_all(events, min_count=1, min_sessions=3)
        self.assertEqual(len(passed), 1)
        self.assertEqual(len(rejected), 0)

    def test_min_sessions_gate_rejects_one_below_threshold(self):
        events = [ev(session=f"s{i}", project="app") for i in range(2)]
        passed, rejected = cluster_all(events, min_count=1, min_sessions=3)
        self.assertEqual(len(passed), 0)
        self.assertEqual(len(rejected), 1)

    def test_frequent_single_session_cluster_does_not_pass(self):
        events = [ev(session="only-one-session", project="app") for _ in range(40)]
        passed, rejected = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(passed, [])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["count"], 40)
        self.assertEqual(rejected[0]["sessions"], 1)


class ScopeTests(unittest.TestCase):
    def test_scope_global_for_two_projects(self):
        events = [
            ev(session="s1", project="app"),
            ev(session="s2", project="fender"),
            ev(session="s3", project="app"),
        ]
        passed, _ = cluster_all(events, min_count=3, min_sessions=3)
        self.assertEqual(passed[0]["scope"], "global")

    def test_scope_project_for_one_project(self):
        events = [ev(session=f"s{i}", project="app") for i in range(3)]
        passed, _ = cluster_all(events, min_count=3, min_sessions=3)
        self.assertEqual(passed[0]["scope"], "project:app")

    def test_scope_global_when_no_named_project(self):
        events = [ev(session=f"s{i}", project="") for i in range(3)]
        passed, _ = cluster_all(events, min_count=3, min_sessions=3)
        self.assertEqual(passed[0]["scope"], "global")
        self.assertEqual(passed[0]["projects"], [])


class NoiseSuppressionTests(unittest.TestCase):
    def test_noise_cause_dropped_by_default(self):
        events = [ev(session=f"s{i}", cause="test or build failure") for i in range(5)]
        passed, rejected = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])

    def test_noise_cause_kept_when_drop_noise_false(self):
        events = [ev(session=f"s{i}", cause="test or build failure") for i in range(5)]
        passed, _ = cluster_all(events, min_count=5, min_sessions=3, drop_noise=False)
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["cause"], "test or build failure")

    def test_noise_causes_constant_includes_expected_exit(self):
        self.assertEqual(NOISE_CAUSES, frozenset({"test or build failure", "expected exit"}))

    def test_expected_exit_dropped_by_default(self):
        events = [ev(session=f"s{i}", cause="expected exit") for i in range(5)]
        passed, rejected = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])

    def test_expected_exit_kept_when_drop_noise_false(self):
        events = [ev(session=f"s{i}", cause="expected exit") for i in range(5)]
        passed, _ = cluster_all(events, min_count=5, min_sessions=3, drop_noise=False)
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["cause"], "expected exit")

    def test_noise_kinds_constant_names_exactly_two_kinds(self):
        self.assertEqual(NOISE_KINDS, frozenset({"retry_identical", "compaction"}))

    def test_retry_identical_kind_dropped_by_default(self):
        events = [ev(session=f"s{i}", kind="retry_identical") for i in range(5)]
        passed, rejected = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])

    def test_retry_identical_kind_kept_when_drop_noise_false(self):
        events = [ev(session=f"s{i}", kind="retry_identical") for i in range(5)]
        passed, _ = cluster_all(events, min_count=5, min_sessions=3, drop_noise=False)
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["kind"], "retry_identical")

    def test_compaction_kind_dropped_by_default(self):
        events = [ev(session=f"s{i}", kind="compaction") for i in range(5)]
        passed, rejected = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])

    def test_compaction_kind_kept_when_drop_noise_false(self):
        events = [ev(session=f"s{i}", kind="compaction") for i in range(5)]
        passed, _ = cluster_all(events, min_count=5, min_sessions=3, drop_noise=False)
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["kind"], "compaction")

    def test_retry_after_error_kind_not_dropped_by_default(self):
        events = [ev(session=f"s{i}", kind="retry_after_error") for i in range(5)]
        passed, _ = cluster_all(events, min_count=5, min_sessions=3)
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["kind"], "retry_after_error")


class ClusterKeyTests(unittest.TestCase):
    def test_key_includes_signature_when_cause_is_other(self):
        event = ev(cause="other", signature="git:128")
        self.assertEqual(cluster_key(event), "tool_error:Bash:other:git:128")

    def test_key_excludes_signature_when_cause_is_informative(self):
        event = ev(cause="command not found", signature="git:128")
        self.assertEqual(cluster_key(event), "tool_error:Bash:command not found")

    def test_cluster_all_splits_other_cause_by_signature(self):
        events = (
            [ev(session=f"a{i}", cause="other", signature="git:128") for i in range(5)]
            + [ev(session=f"b{i}", cause="other", signature="rg:2") for i in range(5)]
        )
        passed, _ = cluster_all(events, min_count=5, min_sessions=3)
        keys = {r["key"] for r in passed}
        self.assertEqual(keys, {
            "tool_error:Bash:other:git:128",
            "tool_error:Bash:other:rg:2",
        })

    def test_record_signature_set_for_other_cause_and_blank_otherwise(self):
        other_events = [ev(session=f"a{i}", cause="other", signature="git:128") for i in range(5)]
        passed, _ = cluster_all(other_events, min_count=5, min_sessions=3)
        self.assertEqual(passed[0]["signature"], "git:128")

        plain_events = [ev(session=f"c{i}", cause="command not found") for i in range(5)]
        passed2, _ = cluster_all(plain_events, min_count=5, min_sessions=3)
        self.assertEqual(passed2[0]["signature"], "")


class ExamplesTests(unittest.TestCase):
    def test_examples_capped_at_three_and_deduplicated(self):
        events = [
            ev(session="s1", detail="same"),
            ev(session="s2", detail="same"),
            ev(session="s3", detail="short"),
            ev(session="s4", detail="a longer one"),
            ev(session="s5", detail="the longest example here"),
        ]
        passed, _ = cluster_all(events, min_count=5, min_sessions=3)
        examples = passed[0]["examples"]
        self.assertEqual(len(examples), 3)
        self.assertEqual(len(examples), len(set(examples)))
        self.assertEqual(examples[0], "same")


class PromoteTests(unittest.TestCase):
    def test_project_scope_promotes_to_global_on_two_projects(self):
        record = {"projects": ["app", "fender"]}
        self.assertEqual(promote("project:app", record), "global")

    def test_global_scope_demotes_to_project_on_one_project(self):
        record = {"projects": ["app"]}
        self.assertEqual(promote("global", record), "project:app")

    def test_scope_unchanged_when_no_drift(self):
        record = {"projects": ["app"]}
        self.assertEqual(promote("project:app", record), "project:app")
        record_global = {"projects": ["app", "fender"]}
        self.assertEqual(promote("global", record_global), "global")


class SortOrderTests(unittest.TestCase):
    def test_cluster_sorts_by_count_descending(self):
        events = (
            [ev(session=f"a{i}", tool="Bash") for i in range(5)]
            + [ev(session=f"b{i}", tool="Read") for i in range(9)]
            + [ev(session=f"c{i}", tool="Write") for i in range(6)]
        )
        result = cluster(events, min_count=5, min_sessions=3)
        self.assertEqual([r["count"] for r in result], [9, 6, 5])


if __name__ == "__main__":
    unittest.main()
