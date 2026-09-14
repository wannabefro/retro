---
name: retro-judge
description: Turns a retro propose judgement brief into a hook or a prose rule, then records it. Use this skill after retro propose emits a brief, or after retro verify names a rule due for a checkpoint.
---

# retro-judge

`retro` never calls a model. This skill is the reasoning step. `retro`
only measures the result and puts it on probation.

Follow these six steps, in order, for one brief.

## 1. Verify the cluster before you believe it

Do this step first. An agent skips this step more than any other step.

Read every sample event and the signature breakdown in the brief. Write
one sentence that states what the cluster actually is.

| You can write that sentence | You cannot write that sentence |
|---|---|
| Go to step 2 | Stop here. The cluster is not a finding |

**Worked example.** During this tool's own build, one cluster held 128
events across 61 sessions. The cluster looked real. But the signature
took only the first word of each shell command. The working directory
resets between tool calls, so almost every command starts with
`cd <path> &&`. The cluster keyed 250 events under `cd`. Only five of
those events were really about `cd`.

## 2. Decide the shape

Ask one question: can a script answer yes or no, with no judgment about
intent?

| Answer | Shape to write |
|---|---|
| Yes | A hook |
| No | Prose |

A hook catches a failure a script can detect alone, such as a banned
command. Prose covers a failure that needs judgment, such as how to
split work across agents.

## 3. Prose must name the number it moves

Name the baseline rate from the brief before you write a prose rule.
Name the rate you expect after the rule ships. A rule with no number
is not a rule `retro verify` can judge.

**Cautionary example.** One prose rule in this author's own config
tells the agent to dispatch independent work in one message. The
brief measured a baseline of 416 of 421 dispatches that broke this
rule. Four days later the rate was 394 of 394 dispatches, a worse
rate. No one noticed, because no one measured it again.

## 4. Choose where the artifact goes

| Scope in the brief | Where you write the artifact |
|---|---|
| `global` | The user's own config directory |
| `project:NAME` | That project's own repository |

Never write to a path that `retro record` protects. Those paths hold
administrator controls and the hooks that stop destructive actions.
When `record` refuses a path, the refusal is correct. Do not write
around a refusal.

## 5. Record the artifact

Run `retro record` after you write the artifact. Give it the cluster
key, the type (`hook` or `prose`), and the path. Add the commit SHA
after you commit the artifact.

Skip this step and the rule stays invisible. `retro verify` cannot
judge a rule it does not know about, and no checkpoint ever reverts it.

## 6. Write one rule, then stop

Write exactly one rule for the brief in front of you. Do not add a
second rule because the first rule feels too small.

A rule that covers two ideas has no single number to move. Split the
two ideas into two clusters, and let `retro propose` measure each one
on its own.

## A verdict is a second entry point

Steps 1 through 6 start from a `retro propose` brief. This section starts
from a verdict that `retro verify` prints, not from a brief.

| Verdict | What the agent does |
|---|---|
| `kept` | Do nothing. The rule earned its place. Say so and stop. |
| `reverted` | Run the `git revert` command from the output. Do not write a new rule in this pass. A live cluster comes back in a later `retro propose`. |
| `unmeasurable` | Do not judge the rule yet. Read the two causes below. |

### Two causes for `unmeasurable`

- **The cluster key matched nothing**. The rule targets something that no
  longer forms a cluster under that key. Leave the rule on probation. Say
  why in your report.
- **The measuring instrument changed**. The result carries
  `rebaseline: true`. The cause list, the suppression rules, or the cluster
  key shape changed since the baseline. The old rate and the new rate are
  not comparable. Record a fresh baseline with `retro record --force` and
  set a new checkpoint. Never delete the old entry. It holds the rule's
  history.

`retro` never re-baselines a rule on its own. A silent re-baseline erases
the rule's history. It also hides the fact that its evidence reset.

## A session review is a third entry point

This entry point starts from one finished session, not from a brief or a
verdict. A `SessionEnd` hook writes one line per session to
`~/.retro/sessions.jsonl`. Read a session that ended with `noisy: true`.
That field names the sessions to read.

Follow these four rules.

1. **One session is evidence of nothing**. A single session is `n=1`.
2. **A session review makes a hypothesis, never a rule**.
3. **Check a hypothesis by counting before it becomes a rule**. State the
   hypothesis as something a script can count. Look for a matching cluster
   in `retro propose`. When `retro propose` shows a matching cluster, judge
   it through the six steps above. When it does not, the hypothesis waits.
   It is not yet a finding.
4. **Never write a rule directly from a session review**.

**Worked example**. During this tool's own build, matching correction words
in a user's own messages produced the wrong answer twice. It flagged the
question "does it fix all the wrong collections?" as a correction from the
user. The sentence is a question about the code, not a correction. Judgment
about one session is real. It is not reliable at `n=1`.
