"""Tests for lib.extract: cause classification, redaction, injection filter, and adapters."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lib import extract
from lib.adapters import claude_code, codex, cursor

FIXTURES = Path(__file__).parent / "fixtures"


def _reset_denylist():
    """Clear the module-level denylist cache by pointing it at a path that cannot exist."""
    missing = str(Path(tempfile.gettempdir()) / "retro-denylist-reset-does-not-exist.txt")
    extract.load_denylist(path=missing)


class ClassifyCauseTests(unittest.TestCase):
    def test_permission_denied_by_user(self):
        self.assertEqual(
            extract.classify_cause("The user denied permission for this tool call."),
            "permission denied by user",
        )

    def test_file_not_found(self):
        self.assertEqual(
            extract.classify_cause("No such file or directory: /tmp/thing.txt"),
            "file not found",
        )

    def test_command_not_found(self):
        self.assertEqual(
            extract.classify_cause("zsh: command not found: catt"),
            "command not found",
        )

    def test_needs_prior_read(self):
        self.assertEqual(
            extract.classify_cause("File has not been read yet. Read it before editing."),
            "needs prior read",
        )

    def test_string_not_found(self):
        self.assertEqual(
            extract.classify_cause("String to replace not found in the file."),
            "string not found",
        )

    def test_string_not_unique(self):
        self.assertEqual(
            extract.classify_cause("String not unique in file. It appears multiple times."),
            "string not unique",
        )

    def test_timeout(self):
        self.assertEqual(
            extract.classify_cause("The command timed out after 120000ms."),
            "timeout",
        )

    def test_too_many_open_files(self):
        self.assertEqual(
            extract.classify_cause("EMFILE: too many open files, open '/tmp/x'"),
            "too many open files",
        )

    def test_git_conflict(self):
        self.assertEqual(
            extract.classify_cause("CONFLICT (content): Merge conflict in file.py"),
            "git conflict",
        )

    def test_test_or_build_failure(self):
        self.assertEqual(
            extract.classify_cause("2 tests failed: test_foo, test_bar"),
            "test or build failure",
        )

    def test_hook_blocked(self):
        self.assertEqual(
            extract.classify_cause("PreToolUse hook blocked this action: disallowed command"),
            "hook blocked",
        )

    def test_other_fallback(self):
        self.assertEqual(
            extract.classify_cause("Something weird happened that does not match any known pattern."),
            "other",
        )


class RedactTests(unittest.TestCase):
    def test_redacts_home_path(self):
        self.assertEqual(extract.redact("/Users/sammctaggart/project/file.py"), "~/project/file.py")

    def test_redacts_token(self):
        text = "leaked token ghp_1234567890abcdefghijklmnopqrstuvwxyz here"
        redacted = extract.redact(text)
        self.assertIn("<redacted>", redacted)
        self.assertNotIn("ghp_1234567890abcdefghijklmnopqrstuvwxyz", redacted)

    def test_redacts_email(self):
        self.assertEqual(extract.redact("contact sam@example.com"), "contact <email>")

    def test_redacts_generic_secret(self):
        text = "key=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        self.assertIn("<redacted>", extract.redact(text))

    def test_redacts_scoped_package_name(self):
        text = '"name": "@acmecorp/reca-browser-runtime"'
        redacted = extract.redact(text)
        self.assertIn("<pkg>", redacted)
        self.assertNotIn("@acmecorp", redacted)

    def test_redacts_internal_hostname(self):
        redacted = extract.redact("connect to build01.ops.internal for the log")
        self.assertIn("<host>", redacted)
        self.assertNotIn("build01.ops.internal", redacted)

    def test_redacts_corp_hostname(self):
        redacted = extract.redact("the endpoint is svc.api.corp right now")
        self.assertIn("<host>", redacted)
        self.assertNotIn("svc.api.corp", redacted)


class DenylistTests(unittest.TestCase):
    def tearDown(self):
        _reset_denylist()

    def test_redacts_term_loaded_from_injected_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            deny_path = Path(tmp) / "redact.txt"
            deny_path.write_text("acmecorp\n# a comment line\n\nwidgetco\n")
            extract.load_denylist(path=str(deny_path))
            redacted = extract.redact("Reported by an AcmeCorp engineer during the incident.")
            self.assertIn("<redacted>", redacted)
            self.assertNotIn("AcmeCorp", redacted)

    def test_absent_denylist_file_yields_empty_list(self):
        missing = str(Path(tempfile.gettempdir()) / "retro-redact-missing-does-not-exist.txt")
        self.assertEqual(extract.load_denylist(path=missing), [])
        self.assertEqual(extract.redact("nothing special here"), "nothing special here")


class ScanForLeaksTests(unittest.TestCase):
    def setUp(self):
        _reset_denylist()

    def tearDown(self):
        _reset_denylist()

    def test_clean_text_finds_nothing(self):
        self.assertEqual(extract.scan_for_leaks(["a plain sentence with no leaks at all"]), [])

    def test_flags_home_path(self):
        findings = extract.scan_for_leaks(["/Users/sammctaggart/project/file.py"])
        self.assertEqual([i for i, _ in findings], [0])

    def test_flags_email(self):
        findings = extract.scan_for_leaks(["contact sam@example.com"])
        self.assertEqual([i for i, _ in findings], [0])

    def test_flags_token(self):
        findings = extract.scan_for_leaks(["ghp_1234567890abcdefghijklmnopqrstuvwxyz"])
        self.assertEqual([i for i, _ in findings], [0])

    def test_flags_package_scope(self):
        findings = extract.scan_for_leaks(['"author": "@acmecorp/i18n-tools"'])
        self.assertEqual([i for i, _ in findings], [0])


class LeakCorpusTests(unittest.TestCase):
    """The redact-then-scan round trip that matters most: modeled on real detail-string leaks."""

    DIRTY = [
        '"name": "@acmecorp/reca-browser-runtime"',
        '"author": "@acmecorp/i18n-tools"',
        "npm ERR! 404 Not Found - GET https://registry.npmjs.org/@acmecorp/design-tokens",
        "added dependency: @acmecorp/eslint-config-base@4.2.0",
        "contact pat.ng@acmecorp.com for access to the repo",
        "export GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "reading /Users/patrickng/dev/app/.env failed: file not found",
        "the api key was sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDE",
        "cwd is /home/ci-runner/workspace/build, retrying step",
        "leaked secret blob a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2 in the diff",
    ]

    def setUp(self):
        _reset_denylist()

    def tearDown(self):
        _reset_denylist()

    def test_redacted_corpus_has_no_remaining_leaks(self):
        redacted = [extract.redact(s) for s in self.DIRTY]
        self.assertEqual(extract.scan_for_leaks(redacted), [])


class IsInjectedTests(unittest.TestCase):
    MARKERS = (
        "<task-notification>",
        "<system-reminder>",
        "<local-command-stdout>",
        "<command-name>",
        "Base directory for this skill:",
        "Caveat: The messages below",
        "This session is being continued",
    )

    def test_each_marker_detected(self):
        for marker in self.MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue(extract.is_injected(marker + " some trailing text"))

    def test_ordinary_text_not_injected(self):
        self.assertFalse(extract.is_injected("please fix the failing test"))

    def test_marker_outside_first_200_chars_not_injected(self):
        padding = "x" * 250
        self.assertFalse(extract.is_injected(padding + "<system-reminder>"))


class DetectTests(unittest.TestCase):
    def _lines(self, name):
        with open(FIXTURES / name) as f:
            return f.readlines()

    def test_claude_detected_from_own_fixture(self):
        self.assertTrue(claude_code.detect(self._lines("claude_sample.jsonl")))

    def test_codex_detected_from_own_fixture(self):
        self.assertTrue(codex.detect(self._lines("codex_sample.jsonl")))

    def test_cursor_detected_from_own_fixture(self):
        self.assertTrue(cursor.detect(self._lines("cursor_sample.jsonl")))

    def test_claude_fixture_not_detected_as_codex_or_cursor(self):
        lines = self._lines("claude_sample.jsonl")
        self.assertFalse(codex.detect(lines))
        self.assertFalse(cursor.detect(lines))

    def test_codex_fixture_not_detected_as_claude_or_cursor(self):
        lines = self._lines("codex_sample.jsonl")
        self.assertFalse(claude_code.detect(lines))
        self.assertFalse(cursor.detect(lines))

    def test_cursor_fixture_not_detected_as_claude_or_codex(self):
        lines = self._lines("cursor_sample.jsonl")
        self.assertFalse(claude_code.detect(lines))
        self.assertFalse(codex.detect(lines))


class ClaudeEventsTests(unittest.TestCase):
    def setUp(self):
        self.path = str(FIXTURES / "claude_sample.jsonl")
        self.events = list(claude_code.events(self.path, since_ts=0.0))

    def test_tool_error_with_command_not_found_cause(self):
        matches = [e for e in self.events if e["kind"] == "tool_error"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["cause"], "command not found")
        self.assertEqual(matches[0]["tool"], "Bash")

    def test_retry_identical_on_repeated_read(self):
        matches = [e for e in self.events if e["kind"] == "retry_identical"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["tool"], "Read")

    def test_user_interrupt(self):
        matches = [e for e in self.events if e["kind"] == "user_interrupt"]
        self.assertEqual(len(matches), 1)

    def test_compaction(self):
        matches = [e for e in self.events if e["kind"] == "compaction"]
        self.assertEqual(len(matches), 1)

    def test_permission_denied_from_tool_denial_kind(self):
        matches = [e for e in self.events if e["kind"] == "permission_denied"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["cause"], "permission denied by user")
        self.assertEqual(matches[0]["tool"], "Edit")

    def test_every_event_matches_frozen_schema(self):
        allowed_kinds = {
            "tool_error", "retry_identical", "retry_after_error",
            "compaction", "user_interrupt", "permission_denied",
        }
        for event in self.events:
            self.assertEqual(event["harness"], "claude")
            self.assertIn(event["kind"], allowed_kinds)
            self.assertLessEqual(len(event["detail"]), 200)
            self.assertIsInstance(event["ts"], float)
            self.assertTrue(os.path.isabs(event["session"]))
            self.assertIn("signature", event)
            self.assertLessEqual(len(event["signature"]), 60)
            self.assertTrue(event["signature"])

    def test_project_from_fixture_cwd(self):
        self.assertEqual(self.events[0]["project"], "project")


class CodexEventsTests(unittest.TestCase):
    def setUp(self):
        self.path = str(FIXTURES / "codex_sample.jsonl")
        self.events = list(codex.events(self.path, since_ts=0.0))

    def test_tool_error_with_test_or_build_failure_cause(self):
        matches = [e for e in self.events if e["kind"] == "tool_error"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["cause"], "test or build failure")

    def test_permission_denied_on_declined_file_change(self):
        matches = [e for e in self.events if e["kind"] == "permission_denied"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["cause"], "permission denied by user")

    def test_retry_identical_on_repeated_exec(self):
        matches = [e for e in self.events if e["kind"] == "retry_identical"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["tool"], "exec_command")

    def test_user_interrupt_from_turn_aborted(self):
        matches = [e for e in self.events if e["kind"] == "user_interrupt"]
        self.assertEqual(len(matches), 1)

    def test_compaction_from_top_level_type(self):
        matches = [e for e in self.events if e["kind"] == "compaction"]
        self.assertEqual(len(matches), 1)

    def test_project_from_session_meta_cwd(self):
        self.assertEqual(self.events[0]["project"], "myproj")


class CursorEventsTests(unittest.TestCase):
    def setUp(self):
        self.path = str(FIXTURES / "cursor_sample.jsonl")
        self.events = list(cursor.events(self.path, since_ts=0.0))

    def test_no_tool_error_even_for_error_shaped_line(self):
        kinds = {e["kind"] for e in self.events}
        self.assertNotIn("tool_error", kinds)
        self.assertNotIn("permission_denied", kinds)
        self.assertNotIn("retry_identical", kinds)

    def test_user_interrupt(self):
        matches = [e for e in self.events if e["kind"] == "user_interrupt"]
        self.assertEqual(len(matches), 1)

    def test_compaction(self):
        matches = [e for e in self.events if e["kind"] == "compaction"]
        self.assertEqual(len(matches), 1)

    def test_only_allowed_kinds_appear(self):
        for event in self.events:
            self.assertIn(event["kind"], ("user_interrupt", "compaction"))
            self.assertEqual(event["harness"], "cursor")


class ExtractEventsIntegrationTests(unittest.TestCase):
    def test_dispatches_to_the_right_adapter_for_each_fixture(self):
        paths = [
            str(FIXTURES / "claude_sample.jsonl"),
            str(FIXTURES / "codex_sample.jsonl"),
            str(FIXTURES / "cursor_sample.jsonl"),
        ]
        events = list(extract.extract_events(paths, since_days=3650))
        harnesses = {e["harness"] for e in events}
        self.assertEqual(harnesses, {"claude", "codex", "cursor"})
        kinds = {e["kind"] for e in events}
        self.assertEqual(
            kinds,
            {"tool_error", "retry_identical", "compaction", "user_interrupt", "permission_denied"},
        )


class RetrySplitTests(unittest.TestCase):
    """Proves retry_after_error vs retry_identical on one synthetic Claude-shaped session."""

    def _write_fixture(self, tmp):
        path = Path(tmp) / "session.jsonl"
        lines = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "flaky-cmd"}}]},
                "timestamp": "2026-01-01T00:00:00.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": "zsh: command not found: flaky-cmd"}]},
                "timestamp": "2026-01-01T00:00:01.000Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "flaky-cmd"}}]},
                "timestamp": "2026-01-01T00:00:02.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "ok now"}]},
                "timestamp": "2026-01-01T00:00:03.000Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/tmp/a.txt"}}]},
                "timestamp": "2026-01-01T00:00:04.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t3", "content": "line one"}]},
                "timestamp": "2026-01-01T00:00:05.000Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t4", "name": "Read", "input": {"file_path": "/tmp/a.txt"}}]},
                "timestamp": "2026-01-01T00:00:06.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t4", "content": "line one"}]},
                "timestamp": "2026-01-01T00:00:07.000Z"},
        ]
        with open(path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        return str(path)

    def test_retry_after_error_and_retry_identical_split_on_same_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            events = list(extract.extract_events([path], since_days=3650))
        after_error = [e for e in events if e["kind"] == "retry_after_error"]
        identical = [e for e in events if e["kind"] == "retry_identical"]
        self.assertEqual(len(after_error), 1)
        self.assertEqual(after_error[0]["tool"], "Bash")
        self.assertEqual(len(identical), 1)
        self.assertEqual(identical[0]["tool"], "Read")


class DiscoverTests(unittest.TestCase):
    def test_filters_by_mtime_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            fresh = root / "fresh.jsonl"
            stale = root / "stale.jsonl"
            fresh.write_text("{}\n")
            stale.write_text("{}\n")
            old_time = time.time() - 40 * 86400
            os.utime(stale, (old_time, old_time))

            found = extract.discover(roots=[root], since_days=30)
            self.assertIn(str(fresh), found)
            self.assertNotIn(str(stale), found)



class DenylistSubstringMatchTests(unittest.TestCase):
    """redact and scan_for_leaks must catch a denylist term with no word boundary around it."""

    DIRTY = [
        "acmecorp_l10n_sdk",
        "acmecorp_internal_tool",
        "foo-acmecorp-bar",
        "ACMECORP_TOKEN",
        "x.acmecorp.com",
        "@acmecorp/pkg",
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        deny_path = Path(self.tmp.name) / "redact.txt"
        deny_path.write_text("acmecorp\n")
        extract.load_denylist(path=str(deny_path))

    def tearDown(self):
        self.tmp.cleanup()
        _reset_denylist()

    def test_redact_removes_the_term_from_every_shape(self):
        for text in self.DIRTY:
            with self.subTest(text=text):
                self.assertNotIn("acmecorp", extract.redact(text).lower())

    def test_scan_for_leaks_flags_every_shape_before_redaction(self):
        for text in self.DIRTY:
            with self.subTest(text=text):
                findings = extract.scan_for_leaks([text])
                self.assertEqual([i for i, _ in findings], [0])


class SignatureOfTests(unittest.TestCase):
    def test_traceback_line_kept_as_is(self):
        text = "Traceback (most recent call last):\n  File more"
        self.assertEqual(extract.signature_of(text), "Traceback (most recent call last):")

    def test_rg_parse_error_kept_as_is(self):
        self.assertEqual(extract.signature_of("rg: regex parse error:\nfoo(bar"), "rg: regex parse error:")

    def test_line_number_and_path_normalized(self):
        text = "zsh:1: no matches found: /private/tmp/claude-502/foo/bar.output"
        self.assertEqual(extract.signature_of(text), "zsh:N: no matches found: P")

    def test_quoted_run_normalized(self):
        text = 'No such tool available: "ctx_batch_execute"'
        self.assertEqual(extract.signature_of(text), "No such tool available: S")

    def test_skips_structural_only_lines(self):
        self.assertEqual(extract.signature_of("{\n[\n   \nreal error line"), "real error line")

    def test_empty_text_is_the_none_marker(self):
        self.assertEqual(extract.signature_of(""), "(none)")

    def test_capped_at_sixty_characters(self):
        self.assertLessEqual(len(extract.signature_of("x" * 200)), 60)


class ProjectResolutionTests(unittest.TestCase):
    def test_cwd_basename_wins_over_the_transcript_slug(self):
        self.assertEqual(extract.resolve_project("/Users/dev/project", "some-long-slug"), "project")

    def test_slug_fallback_recovers_a_short_name_from_an_eighty_char_slug(self):
        slug = "-Users-devuser-acmecorp-Repos-k-repo-worktrees-sm-i18n-feature-13034821"
        self.assertGreaterEqual(len(slug), 60)
        result = extract.resolve_project("", slug)
        self.assertTrue(result)
        self.assertLessEqual(len(result), 40)
        self.assertNotEqual(result, slug)

    def test_leading_dot_segment_survives_the_slug_fallback(self):
        self.assertEqual(extract.resolve_project("", "-Users-devuser--claude"), ".claude")

    def test_result_capped_at_forty_characters(self):
        cwd = "/Users/dev/" + ("x" * 60)
        self.assertEqual(len(extract.resolve_project(cwd)), 40)

    def test_no_cwd_and_no_slug_yields_empty_string(self):
        self.assertEqual(extract.resolve_project("", ""), "")


def _write_jsonl(tmp, lines):
    path = Path(tmp) / "session.jsonl"
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return str(path)


class ClaudeShellSignatureTests(unittest.TestCase):
    def _bash_session(self, command, result_text):
        return [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]},
                "timestamp": "2026-01-01T00:00:00.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": result_text}]},
                "timestamp": "2026-01-01T00:00:01.000Z"},
        ]

    def test_signature_is_binary_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, self._bash_session("git status", "Exit code 128\nfatal: not a git repository"))
            events = list(claude_code.events(path, since_ts=0.0))
        matches = [e for e in events if e["kind"] == "tool_error"]
        self.assertEqual(matches[0]["signature"], "git:128")

    def test_expected_exit_for_a_search_binary_at_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, self._bash_session("rg foo bar.py", "Exit code 1\nno matches"))
            events = list(claude_code.events(path, since_ts=0.0))
        matches = [e for e in events if e["kind"] == "tool_error"]
        self.assertEqual(matches[0]["cause"], "expected exit")
        self.assertEqual(matches[0]["signature"], "rg:1")

    def test_exit_two_from_a_search_binary_stays_a_real_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, self._bash_session("rg [invalid", "Exit code 2\nregex parse error"))
            events = list(claude_code.events(path, since_ts=0.0))
        matches = [e for e in events if e["kind"] == "tool_error"]
        self.assertEqual(matches[0]["cause"], "other")
        self.assertEqual(matches[0]["signature"], "rg:2")


class CodexShellSignatureTests(unittest.TestCase):
    def _exec_session(self, call_id, arguments, item):
        return [
            {"type": "response_item", "timestamp": "2026-01-02T09:00:00.000Z",
             "payload": {"type": "function_call", "call_id": call_id, "name": "exec_command",
                         "arguments": arguments}},
            {"type": "event_msg", "timestamp": "2026-01-02T09:00:01.000Z",
             "payload": {"type": "item_completed", "item": item}},
        ]

    def test_list_form_command_signature_uses_the_invoked_line(self):
        item = {"type": "CommandExecution", "id": "c1", "status": "failed",
                "command": ["/bin/zsh", "-lc", "git status"],
                "stderr": "fatal: not a git repository", "exit_code": 128}
        lines = self._exec_session("c1", '{"command": ["/bin/zsh", "-lc", "git status"]}', item)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, lines)
            events = list(codex.events(path, since_ts=0.0))
        matches = [e for e in events if e["kind"] == "tool_error"]
        self.assertEqual(matches[0]["signature"], "git:128")

    def test_expected_exit_for_diff_at_exit_one(self):
        item = {"type": "CommandExecution", "id": "c1", "status": "failed",
                "command": "diff a.txt b.txt", "stdout": "1c1", "exit_code": 1}
        lines = self._exec_session("c1", '{"command": "diff a.txt b.txt"}', item)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, lines)
            events = list(codex.events(path, since_ts=0.0))
        matches = [e for e in events if e["kind"] == "tool_error"]
        self.assertEqual(matches[0]["cause"], "expected exit")
        self.assertEqual(matches[0]["signature"], "diff:1")


class RetryFalsePositiveTests(unittest.TestCase):
    """The bug: a failed call must not taint an unrelated retry of a different, successful call."""

    def _claude_session(self):
        return [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "flaky-cmd a"}}]},
                "timestamp": "2026-01-01T00:00:00.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": "zsh: command not found: flaky-cmd"}]},
                "timestamp": "2026-01-01T00:00:01.000Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "flaky-cmd b"}}]},
                "timestamp": "2026-01-01T00:00:02.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "ok now"}]},
                "timestamp": "2026-01-01T00:00:03.000Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "flaky-cmd b"}}]},
                "timestamp": "2026-01-01T00:00:04.000Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t3", "content": "ok now"}]},
                "timestamp": "2026-01-01T00:00:05.000Z"},
        ]

    def test_claude_identical_repeat_of_a_different_successful_call_is_retry_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, self._claude_session())
            events = list(claude_code.events(path, since_ts=0.0))
        retry_kinds = [e["kind"] for e in events if e["kind"].startswith("retry")]
        self.assertEqual(retry_kinds, ["retry_identical"])

    def test_claude_extract_events_gives_the_same_result_with_no_post_hoc_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, self._claude_session())
            events = list(extract.extract_events([path], since_days=3650))
        retry_kinds = [e["kind"] for e in events if e["kind"].startswith("retry")]
        self.assertEqual(retry_kinds, ["retry_identical"])

    def test_codex_identical_repeat_of_a_different_successful_call_is_retry_identical(self):
        lines = [
            {"type": "response_item", "timestamp": "2026-01-02T09:00:00.000Z",
             "payload": {"type": "function_call", "call_id": "c1", "name": "exec_command",
                         "arguments": '{"command": "flaky-a"}'}},
            {"type": "event_msg", "timestamp": "2026-01-02T09:00:01.000Z",
             "payload": {"type": "item_completed", "item": {
                 "type": "CommandExecution", "id": "c1", "status": "failed",
                 "stderr": "boom", "exit_code": 1}}},
            {"type": "response_item", "timestamp": "2026-01-02T09:00:02.000Z",
             "payload": {"type": "function_call", "call_id": "c2", "name": "exec_command",
                         "arguments": '{"command": "flaky-b"}'}},
            {"type": "event_msg", "timestamp": "2026-01-02T09:00:03.000Z",
             "payload": {"type": "item_completed", "item": {
                 "type": "CommandExecution", "id": "c2", "status": "completed", "exit_code": 0}}},
            {"type": "response_item", "timestamp": "2026-01-02T09:00:04.000Z",
             "payload": {"type": "function_call", "call_id": "c3", "name": "exec_command",
                         "arguments": '{"command": "flaky-b"}'}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, lines)
            events = list(codex.events(path, since_ts=0.0))
        retry_kinds = [e["kind"] for e in events if e["kind"].startswith("retry")]
        self.assertEqual(retry_kinds, ["retry_identical"])


class CommandHeadTests(unittest.TestCase):
    """command_head must skip cd/export/source/pushd setup clauses to find the real command."""

    def test_cd_then_real_command_with_and(self):
        self.assertEqual(extract.command_head("cd /a/b && rg foo"), "rg")

    def test_cd_then_python_module(self):
        self.assertEqual(extract.command_head("cd /a/b && python3 -m unittest"), "python3")

    def test_export_then_cd_then_script(self):
        result = extract.command_head("export X=1 && cd /a && ./bin/django check")
        self.assertIn(result, ("./bin/django", "bin/django"))

    def test_leading_bare_assignment_then_cd_then_make(self):
        self.assertEqual(extract.command_head("FOO=bar cd /a && make test"), "make")

    def test_bare_cd_alone_is_the_command(self):
        self.assertEqual(extract.command_head("cd /nonexistent"), "cd")

    def test_cd_then_command_separated_by_semicolon(self):
        self.assertEqual(extract.command_head("cd /a/b ; rg foo"), "rg")

    def test_no_setup_prefix_is_unchanged(self):
        self.assertEqual(extract.command_head("git status"), "git")

    def test_binary_of_delegates_to_command_head(self):
        self.assertEqual(extract.binary_of("cd /a && rg foo"), "rg")


class TimeoutClassifyCauseTests(unittest.TestCase):
    def test_harness_timeout_text_classified_as_timeout(self):
        for text in (
            "Command timed out after 2m 0s",
            "Command timed out after 10m 0s",
            "Command timed out after 5m 0s",
        ):
            with self.subTest(text=text):
                self.assertEqual(extract.classify_cause(text), "timeout")


class ProjectForCwdGitTests(unittest.TestCase):
    """project_for_cwd must resolve the repository name, not a worktree branch directory name."""

    def setUp(self):
        extract.reset_project_cache()

    def tearDown(self):
        extract.reset_project_cache()

    def _git(self, cwd, *args):
        subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)

    def test_real_repo_resolves_to_its_own_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo"
            repo.mkdir()
            self._git(str(repo), "init", "-q")
            self.assertEqual(extract.project_for_cwd(str(repo)), "myrepo")

    def test_worktree_resolves_to_the_main_repository_name_not_the_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "k-repo"
            repo.mkdir()
            self._git(str(repo), "init", "-q")
            (repo / "README").write_text("x")
            self._git(str(repo), "add", "README")
            self._git(
                str(repo), "-c", "user.email=a@example.com", "-c", "user.name=a",
                "commit", "-q", "-m", "init",
            )
            worktree = Path(tmp) / "sm-v2-aws-parity"
            self._git(str(repo), "worktree", "add", "-b", "sm-v2-aws-parity", str(worktree))
            self.assertEqual(extract.project_for_cwd(str(worktree)), "k-repo")

    def test_non_repository_directory_falls_back_to_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "not-a-repo"
            plain.mkdir()
            self.assertEqual(extract.project_for_cwd(str(plain)), "not-a-repo")

    def test_nonexistent_path_returns_basename_without_raising(self):
        result = extract.project_for_cwd("/no/such/path/anywhere/myproj")
        self.assertEqual(result, "myproj")

    def test_cache_calls_git_only_once_for_a_repeated_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "cached-repo"
            repo.mkdir()
            self._git(str(repo), "init", "-q")
            with mock.patch("lib.extract.subprocess.run", wraps=subprocess.run) as spy:
                extract.project_for_cwd(str(repo))
                extract.project_for_cwd(str(repo))
                extract.project_for_cwd(str(repo))
            self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
