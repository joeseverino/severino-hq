---
name: hq-pass
disable-model-invocation: true
description: "Run a multi-agent pass on HQ: several hq-worker agents, one worktree each, integrated into one host branch."
argument-hint: "[plan doc]"
---

# A multi-agent pass on HQ

What is particular to HQ when several workers build one host PR.

## 1. Briefs

Each worker is an `hq-worker`. Each brief quotes the decisions it must follow
from the plan doc, and names its `Base SHA:`, which hq-worker checks first.

## 2. Launch

Every worker starts from the pass's base. An extension piece gets its
worktree in the extensions repository, made before the worker launches.

## 3. Integrate

Land the workers on one branch from the base. A rebase conflict in a derived
file is settled by `mise run regen`. A worker whose change reaches templates or
CSS lands only when its report shows `browser:layout` and
`browser:navigation` green on its own tip; one that stopped before them is
resumed or its gates given to the next brief, so the next briefs never start
from a tip nobody has measured.

Then `mise run check` and `REQUIRE_COMPOSED=1 mise run suite:as-image`. A
change to what a page shows gets `/hq-ui-pass` on the integrated branch.
