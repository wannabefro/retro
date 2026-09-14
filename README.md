# retro

`retro` reads finished coding-agent sessions. It finds failures that repeat.
It puts every rule it writes on probation against a measured baseline.

## Running retro

`retro` needs Python 3.10 or later. It needs no third-party packages.

As an installed plugin, run each command with the full plugin path. Claude
Code sets `CLAUDE_PLUGIN_ROOT` for you.

```
$ ${CLAUDE_PLUGIN_ROOT}/bin/retro scan
```

From a clone, run the binary directly from the repository root.

```
$ bin/retro scan
```

You can also put `bin/retro` on your PATH with a symlink.

```
$ ln -s /path/to/retro/bin/retro /usr/local/bin/retro
```

After that, call it as `retro scan` from any directory.

## The probation idea

A rule can look correct and still not work. `retro` measures the failure
rate before it writes a rule, and again after a wait period. When the rate
drops, `retro` keeps the rule. When the rate does not drop, `retro` flags
the rule as reverted. It then names the exact command to undo the change.

## Commands

### `retro scan`

`retro scan` finds recent failures and groups them into ranked clusters.
Use `--days N` to set the lookback window. The default is 30 days.

```
$ retro scan
COUNT  PER-DAY  SESSIONS  SCOPE                   KEY
  134    19.14        57  global                  permission_denied:Bash:permission denied by user
   94    13.43         4  project:app             tool_error:McpToolCall:timeout
```

### `retro propose`

`retro propose` builds a judgement brief for each cluster `retro scan`
finds. A brief holds four parts:

- the cluster record
- up to 12 sample events, each with its own detail and signature
- a breakdown of the signatures inside the cluster
- where a rule for that scope belongs

`retro` never calls a model. `retro propose` only builds the brief. The
agent that reads the brief reasons about it. The `retro-judge` skill
guides that reasoning.

Use `--days N`, `--limit N`, and `--scope global|project:NAME` to narrow
the brief. Add `--json` for machine-readable output.

### `retro record`

`retro record` writes one ledger entry for a rule an agent already wrote.
Pass it `--key KEY`, `--type hook|prose|script`, and `--path PATH`. When
the artifact reverts an earlier rule, also pass `--revert SHA`.

`retro record` measures the current rate for the key, then refuses four
cases.

| Case | Exit code | `--force` overrides it |
|---|---|---|
| The key matches no measurable cluster | 3 | No |
| The key is already on probation | 4 | No |
| The key was reverted before | 5 | Yes |
| The path is protected | 6 | No |

A protected path holds an administrator control or a hook that gates a
destructive action. `retro record` never writes there. `--force` does
not change that refusal.

### `retro session`

`retro session` lists finished sessions from the session log, newest
first. Each row names one session. It shows the end time, the project,
the harness, the event count, whether it was noisy, and its top
signature.

Counting finds what failed. A session can succeed and still go badly.
The noisy rows are the ones worth reading.

Use `--limit N` to cap the row count. The default is 10. Add `--noisy`
to show only the noisy sessions. Add `--json` for machine-readable
output. Use `--path PATH` to read the log from another file.

```
$ retro session
ENDED             PROJECT  HARNESS  EVENTS  NOISY  TOP SIGNATURE
2026-09-13 09:41  retro    claude       31  noisy  tool_error:Bash:command not found
```

`retro session` reads `~/.retro/sessions.jsonl`. A missing log is not an
error. `retro session` prints one line instead: no sessions are recorded
yet. `retro session` skips a malformed line. It does not fail, because
two sessions that end at once can leave a partial line.

### `retro ledger`

`retro ledger` lists every rule and its status. Probation entries come first.

```
$ retro ledger
ID                                 SCOPE   STATUS     BASELINE/DAY  CHECKPOINT  VERDICT
tool-error-bash-command-not-found  global  probation          1.40  2026-09-28
```

### `retro verify`

`retro verify` re-measures each rule that is due for a checkpoint. It compares
the new rate to the rule's own baseline.

```
$ retro verify
ID                                 STATUS     DELTA  BASELINE/DAY  CURRENT/DAY
tool-error-bash-command-not-found  reverted  +12.0%          1.40          1.57
  tool-error-bash-command-not-found: still live. Run this to revert it: git revert 4cea91e
```

`retro verify` never reverts anything. It only prints the `git revert`
command. Even `--apply` never runs `git`. Add `--apply` to write the verdict
into the ledger. When a rule reverts, run the printed command yourself.

The measuring instrument can change since the baseline. Then
`retro verify` refuses a verdict. It marks the rule `unmeasurable` and
names the two versions. It never re-baselines the rule for you. Record a
fresh baseline by hand. The rule then keeps its own history.

## Judging a cluster

The `retro-judge` skill turns one `retro propose` brief into a hook or a
prose rule. It verifies the cluster first, then decides the shape, then
calls `retro record` after it writes the artifact. Run it through the
`/retro propose` command, or call the skill directly.

`retro` measures. The agent reasons. Neither one does the other's job.

## Hooks

`retro` ships two hooks. `hooks/hooks.json` registers both of them. The
plugin starts working the moment you install it.

| Hook | Trigger | What it does |
|---|---|---|
| `session-end.py` | A session finishes. | It profiles that one transcript and appends a line to `~/.retro/sessions.jsonl`. |
| `session-start.py` | A session begins. | When a rule's probation expires, it prints one line and names how many rules are due. |

Neither hook can block a session. Both exit zero on every path, including
on malformed input.

`session-end.py` profiles one transcript, never the whole history. It is
fast for that reason. It writes nothing to standard output.

`session-start.py` is the scheduler. There is no cron job and no daemon.
The next session you start is what triggers a due verdict.

## Cursor transcripts

A Cursor transcript carries no tool results. `retro` can extract only two
event kinds from it: a user interrupt and a compaction. Every other signal
comes from Claude Code and Codex transcripts.

## JSON output

Add `--json` to `scan`, `propose`, `session`, `ledger`, or `verify`
for machine-readable output.

## What this does not do

`retro` never reverts a commit. It prints the command instead.

`retro` never calls a model. An agent reads a brief and reasons. The
tool only measures.

A cluster can be an artefact of how events are grouped. It is not
always a real pattern. For that reason, the judge skill verifies a
cluster before it writes a rule.
