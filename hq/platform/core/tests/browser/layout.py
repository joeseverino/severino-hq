"""The layout rules held over the stress estate, which the sweeps run."""

import json
from time import perf_counter
from typing import override

from hq.platform.core.tests.browser.gate import RULE_COSTS, SET_ASIDE, BrowserGate, signature
from hq.platform.core.tests.browser.probes.settle import FINISH_TRANSITIONS, SETTLE
from hq.platform.core.tests.browser.probes.tables import CELLS, TABLES_THAT_DO_NOT_FIT
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.stress import stress_estate
from hq.platform.core.tests.browser.survey import CLOSE_OPENED, open_every_fold

# A key that changes nothing, pressed before a batch a gate opens, so the
# browser takes what the batch moves as the reader's own doing (`as_a_reader`).
_NO_OP_KEY = "Shift"

# A page's note on its readings, saying nothing and then the longest line it
# says (a failure that names what it could not read), against the band
# between the bar and the page: where it stands, how far its end is from the
# end of the person's control, and where each part of the page is with it
# silent, saying that, and gone; and whether each sentence its slot keeps room
# for (its struts) is drawn whole. The note is left saying what it said.
_PAGE_NOTE = """([noteSelector, lineSelector, contentSelector]) => [...document.querySelectorAll(noteSelector)].map((note) => {
  const content = document.querySelector(contentSelector);
  const line = note.closest(lineSelector);
  // Every part of the page but the note, where it lands.
  const parts = () => [...line.children, ...content.querySelectorAll(':scope > *, :scope > * > *')]
    .filter((part) => part !== note && part !== line && part.getClientRects().length)
    .map((part) => { const r = part.getBoundingClientRect(); return [r.top, r.left, r.width, r.height].map(Math.round).join(' '); });
  const said = note.textContent;
  const cut = [...note.parentElement.children].filter((strut) => strut.dataset.strut).map((strut) => {
    note.textContent = strut.dataset.strut;
    return note.scrollWidth > note.clientWidth + 0.5 ? strut.dataset.strut : null;
  }).filter(Boolean);
  note.textContent = '';
  const silent = parts();
  note.textContent = 'Cannot refresh: ' + Array.from({length: 24}, (_, at) => `example reading ${at}`).join(', ') + '.';
  const saying = parts();
  const r = note.getBoundingClientRect();
  const box = line.getBoundingClientRect();
  const placed = {top: r.top - box.top, bottom: box.bottom - r.bottom, start: r.left - box.left, end: box.right - r.right,
    lines: note.scrollHeight > note.clientHeight + 0.5};
  const [slot, sibling] = [note.parentElement, note.nextSibling];
  note.remove();
  const gone = parts();
  slot.insertBefore(note, sibling);
  note.textContent = said;
  return {silent, saying, gone, placed, cut};
})"""


class LayoutBrowserTests(BrowserGate):
    """The layout rules over the stress estate.

    Each page is opened once per width and per configuration (script off,
    script on), and every rule that holds there is read off that one load,
    each in a subtest named for the rule, the page and the width. The rules
    that change the page (open its disclosures, dispatch a resize) run after
    the ones that only read it.

    The tests are the classes below, a sweep or a share of one each: Django
    hands a parallel worker a whole class, so they run side by side, and a
    run lasts as long as its longest class.
    """

    lists: tuple[str, ...]

    @classmethod
    @override
    def render(cls):
        pages, cls.lists = stress_estate()
        return pages

    def loaded(self, name, width, **where):
        """Open ``name`` at ``width``; one that does not load fails once, not once per rule."""

        # subTest records a failure and carries on past the block, with nothing opened.
        opened: list[bool] = []
        with self.subTest(page=name, width=width, **where):
            self.open(name, width)
            opened.append(True)
        return bool(opened)

    def hold(self, name, width, rules, **where):
        """Hold each rule on the page as it stands, one subtest each, in
        order, each subtest's time going to the run's ledger."""

        for rule in rules:
            began = perf_counter()
            try:
                with self.subTest(rule=rule.__name__, page=name, width=width, **where):
                    rule(name)
            finally:
                self.spent(RULE_COSTS, rule.__name__, perf_counter() - began, f"{name} at {width}px")

    def hold_opened(self, name, width, *, popovers=True, **where):
        """Every layout check with every fold open as the survey opens them
        (`open_every_fold`: with script, every row a reader can open too), and
        with each anchored popover shown when ``popovers``, wherever the page
        holds something closed: with nothing to open, the page open is the
        page as read. What was opened is closed again after."""

        self.folds_opened = 0
        opened = [self.opens_every_fold]
        if popovers:
            opened.append(self.every_anchored_popover_keeps_the_layout_rules)
        self.hold(name, width, opened, disclosures="open", **where)
        if self.folds_opened:
            self.page.evaluate(CLOSE_OPENED)
            self.settle()

    def opens_every_fold(self, name):
        """Every fold open, round after round, and every layout check held on
        the page so: a fold that would not open, or a region it brought in
        that never answered, is a finding of its own."""

        self.folds_opened, short = open_every_fold(self)
        if short:
            self.fail(f"opening every fold stopped short: {short}")
        if self.folds_opened:
            self.every_layout_check_holds(name)

    def open_every_disclosure(self, _name):
        """Every disclosure open, for the rules that follow. A popover is
        shown alone beside its opener instead (``each_popover_kind``),
        as a reader opens one."""

        self.as_a_reader("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")

    def every_anchored_popover_keeps_the_layout_rules(self, name):
        """Every check the layout rules make, with each popover hung from an
        anchor shown in turn beside its opener."""

        width = self.page.viewport_size["width"]
        if found := self.with_each_anchored_popover(lambda: self.findings(name, width)):
            self.fail(json.dumps(found, indent=1))

    def with_each_anchored_popover(self, read):
        """What ``read`` finds, by check, with one popover of each kind hung
        from an anchor shown beside its opener (``each_popover_kind``), past
        what it finds with that popover alone hidden again, the view left
        where its opener brought it: a sticky bar there lies over what
        scrolled beneath it, which is the page's and judged at the top."""

        found = {}
        for (label, _slug), _alike in self.each_popover_kind(anchored=True):
            # Finished rather than waited for: a page without script runs no frame.
            self.page.evaluate(FINISH_TRANSITIONS)
            shown = read()
            self.page.evaluate(SET_ASIDE)
            beneath = {signature(check, line) for check, lines in read().items() for line in lines}
            for check, lines in shown.items():
                if added := [line for line in lines if signature(check, line) not in beneath]:
                    found[f"{check}, with {label} shown"] = added
        return found

    def as_a_reader(self, script, arg=None):
        """``script`` run as a reader's press: a script's click is no input
        to the browser, so Chromium would score what the batch pushes down
        as the page moving by itself. A trusted key first, which changes
        nothing, lets its rule for the half second after an input (Layout
        Instability, `hadRecentInput`) excuse the batch, as it excuses each
        of a reader's taps; a shift that lands later is still the page's."""

        self.page.keyboard.press(_NO_OP_KEY)
        return self.page.evaluate(script, arg)

    def a_heads_note_costs_its_head_no_room(self, name):
        """A page's note on its readings ends the head's first line, inside
        it and on one line however long what it says: the page is laid out as
        it is with no note at all, saying something moves nothing, and each
        sentence HQ writes for it word for word is drawn whole."""

        notes = self.page.evaluate(
            _PAGE_NOTE, self.css([SELECTORS["page_note"], SELECTORS["page_trail_row"], SELECTORS["content"]])
        )
        if 'form="page-visit"' in self.pages[name]:
            self.assertGreater(len(notes), 0, "the probe measured nothing")
        for note in notes:
            self.assertEqual(note["silent"], note["gone"])
            self.assertEqual(note["saying"], note["gone"])
            placed = note["placed"]
            self.assertGreaterEqual(placed["top"], -0.5)
            self.assertGreaterEqual(placed["bottom"], -0.5)
            self.assertGreaterEqual(placed["start"], -0.5)
            self.assertGreaterEqual(placed["end"], -0.5)
            self.assertFalse(placed["lines"])
            self.assertEqual(note["cut"], [])

    def nothing_runs_out_of_its_table_cell(self, _name):
        self.assertEqual(self.read([(CELLS, None)])[0], [])

    def every_palette_check_holds(self, _name):
        """Text and the edges of fields and buttons against what they sit
        on, in the palette the page is drawn in."""

        if found := {check: said for check, said in self.palette().items() if said}:
            self.fail(json.dumps(found, indent=1))

    def every_layout_check_holds(self, name):
        """Every check the layout rules make, on the page as a reader has it:
        what `dev:look --rule survey` reports, held."""

        if found := self.findings(name, self.page.viewport_size["width"]):
            self.fail(json.dumps(found, indent=1))

    # With script: the page a person reads, sorted and selectable.

    def a_table_fits_its_box(self, name):
        """A table never overflows its box at the width the page stands at:
        while it is narrower than its columns' floors, its ranked columns
        leave, then its controls stand beneath its words (tables.css). Every
        table on the page, with every disclosure open."""

        self.open_every_disclosure(name)
        self.page.evaluate(SETTLE)
        self.assertEqual(self.page.evaluate(TABLES_THAT_DO_NOT_FIT, self.css(SELECTORS["table_scroll"]))["found"], [])

    def a_table_fits_its_box_at_every_width(self, name):
        """Every table on the page, with every disclosure open, fits its box
        and keeps every table check, its rows opened, at every window width
        from the narrowest phone up."""

        self.open_every_disclosure(name)
        self.page.evaluate(SETTLE)
        self.assertEqual(self.table_defects_at_every_width(), {})

    def card_rows(self, cards):
        """The cards of one dashboard grid, grouped by the row they sit on."""
        rows: dict[int, list[dict]] = {}
        for box in self.page.locator(self.css(cards)).evaluate_all(
            "elements => elements.map(e => { const r = e.getBoundingClientRect(); return {top: r.top, left: r.left}; })"
        ):
            rows.setdefault(round(box["top"]), []).append(box)
        return list(rows.values())
