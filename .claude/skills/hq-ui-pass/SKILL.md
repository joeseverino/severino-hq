---
name: hq-ui-pass
description: "Use whenever a change touches templates/, static/css, static/js or what a page shows: look at every page it reaches with dev:look, closed and opened, and fix and look again until the survey of every page exits 0."
---

# Look at it before saying it's done

A gate catches classes of mistake; it does not see whether a page reads well.
Only looking does, and it is done before the change is handed on.

1. **Criteria.** Read the Rules in `docs/Design.md#rules`.
2. **Scope.** List every page the change reaches: the views that render the
   changed templates, partials, CSS and scripts, not only the one you edited.
3. **Look.** In your worktree, at `dev:look`'s default widths, which the gate
   shares:

   ```sh
   mise run dev:look <pages>
   mise run dev:look <pages> --expand --open
   ```

   Read every screenshot under `var/look/`. The pages `dev:look` names and
   its options are in `docs/Development.md#browser-layout`. A page or
   state it does not draw: run `mise run dev:scratch --seed showcase` and take
   a session from `mise run dev:session`.

   A change to how something sizes (widths, wrapping, intrinsic sizing) is
   looked at on the dense pages too, since a rule that fits the estate can
   overflow a dense band. Run `--rule survey` after each such change, and
   commit it only once `mise run browser:layout` passes on it.

   A layout failure CI's Linux browsers show and a Mac does not is
   reproduced locally by widening text the way Linux fonts do:
   `mise run dev:look <page> --css 'main { letter-spacing: .02em }'`.
4. **Judge.** Write down the five worst things, by the rules: dead whitespace
   or a gulf in a row; uneven heights or edges side by side; a card inside a
   card; a header on two rows or a wrapped chip; a table wider than its box;
   something cut or folded while its row has room; a fact said twice; a
   sentence that restates the page.
5. **Fix and look again** at the shared primitive (partial, CSS rule), never
   the page, until the list is empty. The pass is finished when
   `mise run dev:look all --rule survey` exits 0: both seeds, the demo estate
   and the dense one, on every page. A finding on a page you did not touch is
   yours too: fix it, or stop and say so, never hand it back as outside your
   files. `--base <your base>` prints only what your change broke and what it
   fixed, for reading a long list.
6. **Guard.** A kind of problem that has turned up before becomes a rule in
   `hq/platform/core/browser_tests.py` that measures everything of its kind a
   reader sees, every element on the whole page and on every page the estate
   draws, never a selector list of where the defect turned up. It lands with
   proof it can fail, on a page the change touched and on at least one it did
   not: on each, `mise run dev:look <page> --rule <rule> --css '<the defect>'`
   fails, and without `--css` holds. Paste them all. A gate's green covers a
   defect only where its rules reach, so a defect counts as guarded when a
   named rule measures it.

Report what the screenshots showed and what changed, never "looks good".
