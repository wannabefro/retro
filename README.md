# retro

`retro` reads finished coding-agent sessions. It finds failures that repeat.
It puts every rule it writes on probation against a measured baseline.

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
tool-error-bash-command-not-found  reverted  +12.0%          1.40         1.57
  tool-error-bash-command-not-found: still live. Run this to revert it: git revert 4cea91e
```

`retro verify` never reverts anything. It only prints the `git revert`
command. Even `--apply` never runs `git`. Add `--apply` to write the verdict
into the ledger. When a rule reverts, run the printed command yourself.

The measuring instrument can change since the baseline. Then
`retro verify` refuses a verdict. It marks the rule `unmeasurable` and
names the two versions. It never re-baselines the rule for you. Record a
fresh baseline by hand. The rule then keeps its own history.

## Cursor transcripts

A Cursor transcript carries no tool results. `retro` can extract only two
event kinds from it: a user interrupt and a compaction. Every other signal
comes from Claude Code and Codex transcripts.

## JSON output

Add `--json` to `scan`, `ledger`, or `verify` for machine-readable output.
