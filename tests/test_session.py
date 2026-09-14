"""Tests for lib.session and the session-end/session-start hooks."""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lib import extract, ledger, session

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSION_END_PATH = os.path.join(_ROOT, "hooks", "session-end.py")
_SESSION_START_PATH = os.path.join(_ROOT, "hooks", "session-start.py")
_BIN_RETRO_PATH = os.path.join(_ROOT, "bin", "retro")


def _reset_denylist():
    """Point the redaction denylist at a path that cannot exist, isolating tests from the machine's own."""
    missing = str(Path(tempfile.gettempdir()) / "retro-session-denylist-missing.txt")
    extract.load_denylist(path=missing)


def _load_module(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_jsonl(tmp, lines):
    path = os.path.join(tmp, "session.jsonl")
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _tool_use(idx, ts, tool="Bash", command="ls"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"c{idx}", "name": tool, "input": {"command": command}}]},
        "timestamp": ts}


def _tool_error(idx, ts_call, ts_result, text, tool="ReadFile"):
    return [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{idx}", "name": tool, "input": {"path": f"p{idx}"}}]},
            "timestamp": ts_call},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{idx}", "is_error": True, "content": text}]},
            "timestamp": ts_result},
    ]


def _minute(i):
    return f"2026-01-01T00:{i:02d}:00.000Z"


class ProfileCountsAndOrderTests(unittest.TestCase):
    def setUp(self):
        _reset_denylist()

    def tearDown(self):
        _reset_denylist()

    def test_counts_kinds_and_top_signatures_order(self):
        """Six error groups of decreasing size must rank highest-first, capped at five."""
        groups = [("Widget mismatch alpha", 6), ("Widget mismatch beta", 5),
                  ("Widget mismatch gamma", 4), ("Widget mismatch delta", 3),
                  ("Widget mismatch epsilon", 2), ("Widget mismatch zeta", 1)]
        lines = []
        idx = 0
        minute = 0
        for text, count in groups:
            for _ in range(count):
                lines.extend(_tool_error(idx, _minute(minute), _minute(minute), text))
                idx += 1
                minute += 1
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, lines)
            result = session.profile(path)
        self.assertEqual(result["events"], 21)
        self.assertEqual(result["kinds"], {"tool_error": 21})
        self.assertEqual(result["top_signatures"], [
            ["Widget mismatch alpha", 6], ["Widget mismatch beta", 5],
            ["Widget mismatch gamma", 4], ["Widget mismatch delta", 3],
            ["Widget mismatch epsilon", 2],
        ])

    def test_session_field_is_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, _tool_error(0, _minute(0), _minute(0), "boom"))
            result = session.profile(path)
        self.assertEqual(result["session"], os.path.abspath(path))
        self.assertTrue(os.path.isabs(result["session"]))


class NoisyThresholdTests(unittest.TestCase):
    def setUp(self):
        _reset_denylist()

    def tearDown(self):
        _reset_denylist()

    def _non_suppressed_transcript(self, count):
        lines = []
        for i in range(count):
            lines.extend(_tool_error(i, _minute(i), _minute(i), f"Distinct failure {i}"))
        return lines

    def test_noisy_true_exactly_at_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, self._non_suppressed_transcript(session.NOISY_EVENT_FLOOR))
            result = session.profile(path)
        self.assertEqual(result["events"], 10)
        self.assertTrue(result["noisy"])

    def test_noisy_false_exactly_one_below_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, self._non_suppressed_transcript(session.NOISY_EVENT_FLOOR - 1))
            result = session.profile(path)
        self.assertEqual(result["events"], 9)
        self.assertFalse(result["noisy"])

    def test_noisy_ignores_twenty_suppressed_retry_events(self):
        """21 identical calls in a row yield 20 retry_identical events, all suppressed."""
        lines = [_tool_use(i, _minute(i)) for i in range(21)]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, lines)
            result = session.profile(path)
        self.assertEqual(result["events"], 20)
        self.assertEqual(result["kinds"], {"retry_identical": 20})
        self.assertFalse(result["noisy"])


class AppendProfileTests(unittest.TestCase):
    def test_appends_rather_than_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.jsonl")
            session.append_profile({"session": "a"}, path)
            session.append_profile({"session": "b"}, path)
            with open(path) as f:
                lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["session"], "a")
        self.assertEqual(json.loads(lines[1])["session"], "b")

    def test_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "sessions.jsonl")
            session.append_profile({"session": "a"}, path)
            self.assertTrue(os.path.isfile(path))


class DueSummaryTests(unittest.TestCase):
    def _rule(self, rule_id, checkpoint, status="probation"):
        return {"id": rule_id, "status": status, "checkpoint_at": checkpoint}

    def test_counts_and_lists_only_due_probation_entries(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        data = {"rules": [
            self._rule("a", "2026-05-01T00:00:00Z"),
            self._rule("b", "2026-05-15T00:00:00Z"),
            self._rule("c", "2026-07-01T00:00:00Z"),
            self._rule("d", "2026-05-01T00:00:00Z", status="kept"),
        ]}
        self.assertEqual(session.due_summary(data, now=now), {"due": 2, "ids": ["a", "b"]})

    def test_nothing_due_yields_zero_and_empty_ids(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        data = {"rules": [self._rule("a", "2026-07-01T00:00:00Z")]}
        self.assertEqual(session.due_summary(data, now=now), {"due": 0, "ids": []})


class SessionEndFunctionTests(unittest.TestCase):
    """Function-level checks on the loaded hook module, isolated from the real home directory."""

    def setUp(self):
        self.module = _load_module(_SESSION_END_PATH, "retro_session_end")

    def test_missing_transcript_key_exits_zero_without_writing(self):
        called = mock.Mock()
        with mock.patch.object(self.module, "append_profile", called):
            code = self.module.run(stdin=io.StringIO(json.dumps({"cwd": "/tmp"})))
        self.assertEqual(code, 0)
        called.assert_not_called()

    def test_nonexistent_transcript_path_exits_zero_without_writing(self):
        called = mock.Mock()
        with mock.patch.object(self.module, "append_profile", called):
            payload = json.dumps({"transcript_path": "/no/such/file.jsonl"})
            code = self.module.run(stdin=io.StringIO(payload))
        self.assertEqual(code, 0)
        called.assert_not_called()

    def test_session_id_fallback_resolves_under_claude_projects(self):
        with tempfile.TemporaryDirectory() as home:
            projects = Path(home) / ".claude" / "projects" / "some-project"
            projects.mkdir(parents=True)
            transcript = projects / "abc123.jsonl"
            transcript.write_text("")
            with mock.patch.dict(os.environ, {"HOME": home}):
                found = self.module._resolve_transcript({"session_id": "abc123"})
            self.assertEqual(found, str(transcript))

    def test_no_transcript_and_no_session_id_resolves_to_none(self):
        self.assertIsNone(self.module._resolve_transcript({}))

    def test_discover_is_never_called_and_profile_is_still_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_jsonl(tmp, _tool_error(0, _minute(0), _minute(0), "boom"))
            sessions_path = os.path.join(tmp, "sessions.jsonl")
            payload = json.dumps({"transcript_path": path})
            with mock.patch("lib.extract.discover", side_effect=AssertionError("must not be called")), \
                 mock.patch("lib.session.DEFAULT_SESSIONS_PATH", sessions_path):
                code = self.module.run(stdin=io.StringIO(payload))
            self.assertEqual(code, 0)
            with open(sessions_path) as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 1)


class SessionEndSubprocessTests(unittest.TestCase):
    """Runs the real script as a subprocess, feeding stdin, so a hang-on-real-input bug would show."""

    def _run(self, stdin_text, home):
        env = dict(os.environ)
        env["HOME"] = home
        env.pop("RETRO_DEBUG", None)
        return subprocess.run(
            [sys.executable, _SESSION_END_PATH], input=stdin_text,
            capture_output=True, text=True, env=env, timeout=10,
        )

    def test_empty_stdin_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run("", home)
        self.assertEqual(proc.returncode, 0)

    def test_invalid_json_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run("not json", home)
        self.assertEqual(proc.returncode, 0)

    def test_missing_transcript_key_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(json.dumps({"hook_event_name": "SessionEnd"}), home)
        self.assertEqual(proc.returncode, 0)

    def test_nonexistent_transcript_path_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(json.dumps({"transcript_path": "/no/such/file.jsonl"}), home)
        self.assertEqual(proc.returncode, 0)

    def test_successful_run_writes_nothing_to_stdout(self):
        with tempfile.TemporaryDirectory() as home:
            transcript = _write_jsonl(home, _tool_error(0, _minute(0), _minute(0), "boom"))
            proc = self._run(json.dumps({"transcript_path": transcript}), home)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            sessions_path = os.path.join(home, ".retro", "sessions.jsonl")
            self.assertTrue(os.path.isfile(sessions_path))


class SessionStartFunctionTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_module(_SESSION_START_PATH, "retro_session_start")

    def test_nothing_due_prints_nothing(self):
        with mock.patch.object(self.module.ledger, "load", return_value={"rules": []}):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.module.run()
        self.assertEqual(buf.getvalue(), "")

    def test_two_due_prints_one_line_naming_the_count(self):
        data = {"rules": [
            {"id": "a", "status": "probation", "checkpoint_at": "2000-01-01T00:00:00Z"},
            {"id": "b", "status": "probation", "checkpoint_at": "2000-01-01T00:00:00Z"},
        ]}
        with mock.patch.object(self.module.ledger, "load", return_value=data):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.module.run()
        output = buf.getvalue()
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("2", lines[0])
        self.assertIn("retro verify", lines[0])
        self.assertLess(len(lines[0]), 120)


class SessionStartSubprocessTests(unittest.TestCase):
    """Runs the real script as a subprocess, feeding stdin even though this hook ignores it."""

    def _run(self, home):
        env = dict(os.environ)
        env["HOME"] = home
        env.pop("RETRO_DEBUG", None)
        return subprocess.run(
            [sys.executable, _SESSION_START_PATH], input="{}",
            capture_output=True, text=True, env=env, timeout=10,
        )

    def test_no_ledger_prints_nothing_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_two_due_rules_prints_one_line_with_no_expanded_home_path(self):
        with tempfile.TemporaryDirectory() as home:
            retro_dir = os.path.join(home, ".retro")
            os.makedirs(retro_dir)
            data = {"rules": [
                {"id": "a", "status": "probation", "checkpoint_at": "2000-01-01T00:00:00Z"},
                {"id": "b", "status": "probation", "checkpoint_at": "2000-01-01T00:00:00Z"},
            ]}
            with open(os.path.join(retro_dir, "ledger.json"), "w") as f:
                json.dump(data, f)
            proc = self._run(home)
        self.assertEqual(proc.returncode, 0)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("2", lines[0])
        self.assertNotIn(home, proc.stdout)
        self.assertNotIn("/Users", proc.stdout)


class SessionCommandTests(unittest.TestCase):
    """Tests for `retro session`, loaded from the real bin/retro script."""

    def setUp(self):
        self.module = _load_module(_BIN_RETRO_PATH, "retro_bin_for_session_tests")

    def _run(self, argv):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = self.module.main(argv)
        return code, buf.getvalue()

    def _write_log(self, tmp, lines):
        path = os.path.join(tmp, "sessions.jsonl")
        with open(path, "w") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return path

    def _profile(self, session_id, ended_at, noisy=False):
        return {
            "session": session_id, "harness": "claude-code", "project": "retro",
            "ended_at": ended_at, "events": 5, "kinds": {}, "top_signatures": [["boom", 5]],
            "noisy": noisy,
        }

    def test_missing_log_exits_zero_with_readable_message(self):
        code, out = self._run(["session", "--path", "/no/such/sessions.jsonl"])
        self.assertEqual(code, 0)
        self.assertIn("no sessions", out.lower())

    def test_limit_returns_newest_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [self._profile(f"s{i}", f"2026-01-0{i}T00:00:00Z") for i in range(1, 6)]
            path = self._write_log(tmp, lines)
            code, out = self._run(["session", "--path", path, "--limit", "2", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([p["session"] for p in payload], ["s5", "s4"])

    def test_noisy_filters_to_noisy_sessions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [
                self._profile("quiet", "2026-01-01T00:00:00Z", noisy=False),
                self._profile("loud", "2026-01-02T00:00:00Z", noisy=True),
            ]
            path = self._write_log(tmp, lines)
            code, out = self._run(["session", "--path", path, "--noisy", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([p["session"] for p in payload], ["loud"])

    def test_malformed_line_is_skipped_and_valid_lines_still_render(self):
        """A partial line from a concurrent writer must not crash the whole read."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_log(tmp, [
                self._profile("first", "2026-01-01T00:00:00Z"),
                "{not valid json",
                self._profile("second", "2026-01-02T00:00:00Z"),
            ])
            code, out = self._run(["session", "--path", path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual({p["session"] for p in payload}, {"first", "second"})

    def test_json_flag_emits_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_log(tmp, [self._profile("a", "2026-01-01T00:00:00Z")])
            code, out = self._run(["session", "--path", path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["session"], "a")

    def test_human_table_lines_stay_under_100_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_log(tmp, [self._profile("a", "2026-01-01T00:00:00Z")])
            code, out = self._run(["session", "--path", path])
        self.assertEqual(code, 0)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 100)


class HooksManifestTests(unittest.TestCase):
    def test_manifest_names_both_hook_scripts_and_they_exist_on_disk(self):
        """A renamed hook script must not silently unregister itself from the manifest."""
        with open(os.path.join(_ROOT, "hooks", "hooks.json")) as f:
            manifest = json.load(f)
        commands = [
            hook["command"]
            for entries in manifest["hooks"].values()
            for entry in entries
            for hook in entry["hooks"]
        ]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/session-end.py", commands)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/session-start.py", commands)
        for command in commands:
            relative = command.replace("${CLAUDE_PLUGIN_ROOT}/", "")
            self.assertTrue(os.path.isfile(os.path.join(_ROOT, relative)), relative)


if __name__ == "__main__":
    unittest.main()
