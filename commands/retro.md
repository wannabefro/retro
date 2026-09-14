---
description: Run `retro scan`, or hand a `retro propose` or `retro verify` brief to the retro-judge skill
argument-hint: "[propose|verify] [--days N] [--limit N] [--scope global|project:NAME] [--json]"
---

Route on `$ARGUMENTS`. Run every command as `${CLAUDE_PLUGIN_ROOT}/bin/retro`.

| Argument | Action |
|---|---|
| none | Run `${CLAUDE_PLUGIN_ROOT}/bin/retro scan`. Print the ranked clusters. |
| `propose` | Run `${CLAUDE_PLUGIN_ROOT}/bin/retro propose`, passing through any extra flags. For each brief it emits, invoke the `retro-judge` skill. |
| `verify` | Run `${CLAUDE_PLUGIN_ROOT}/bin/retro verify`. For each rule the output marks due, invoke the `retro-judge` skill. |

This command only dispatches. The `retro-judge` skill holds the judgement
steps. Do not repeat those steps here.

`${CLAUDE_PLUGIN_ROOT}/bin/retro verify` never reverts a rule. It only prints
the `git revert` command. Run that command yourself when a rule is due for
reversion.
