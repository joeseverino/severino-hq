---
name: hq-house-rules
description: HQ's rules for one worker building a piece of HQ or an extension in a worktree, on top of the general house rules and AGENTS.md. Preloaded by hq-worker.
user-invocable: false
---

# HQ house rules

These add to the house rules and AGENTS.md and never repeat them. HQ is
public; its extensions live in a private repository of their own. The main
session integrates every piece into one host PR.

## Tests

Fewer, stronger tests: no exact query counts (assert "does not grow with N" or
a generous ceiling), no wall-clock bounds or sleeps (use events or joins), no
exact long strings, markup or class lists (assert the fact, the link or the
`data-` hook), no word scans.

## Gates

In HQ the gate is `mise run fast <base>`, with the base your brief names
(`docs/Development.md#the-gates` says what it selects). In the extensions
repository it is that repository's `check` task for each package changed,
after its `fix` task: a package's own suite misses the lint, format and
template contract the check holds.

`fast` already runs every check its selection reaches, so a single gate
(`mise run checks:mypy`) is only for iterating on a failure it named. While
iterating on a failed test, rerun only that test or class by label
(`mise run test <label>`, which a browser class takes too); a whole gate, the
browser gates above all, runs once the fix holds.

`mise run test --at <ref> <labels>` runs the current tests against the code at
`<ref>`: the way to prove a regression test fails without the fix.

## Scratch scripts

HQ's gitignored scratch path is the worktree's `var/`: `mkdir -p var` first.
