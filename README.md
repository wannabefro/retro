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
COUNT  PER-DAY  SESSIONS  SCOPE        KEY
   42     1.40        11  global       tool_error:Bash:command not found
    8     0.27         5  project:app  retry_identical:Edit:same string not found
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

`retro verify` does not revert anything by itself. Add `--apply` to write
the verdict into the ledger. Even then, `retro verify` never runs `git`.
When a rule reverts, run the printed `git revert` command yourself.

## JSON output

Add `--json` to `scan`, `ledger`, or `verify` for machine-readable output.
