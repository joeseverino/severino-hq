"""No template draws by hand what a shared part draws (docs/Design.md).

The shapes and the part to write for each are one table,
``hq_sdk.template_contract.HAND_DRAWN``, which ``hq plugin check`` holds an
extension's templates to, with the package's own ``hand_drawn.json`` as its
baseline. This reads the same table over the host's templates: one that
draws a shape is either a part itself (``PARTS``) or one of today's pages
still to move onto its part (``hand_drawn_baseline``), whose counts only fall.
"""

from collections import Counter
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq_sdk.template_contract import HAND_DRAWN
from hq_sdk.validation import hand_drawn

from .hand_drawn_baseline import BASELINE
from .source import source_files

HOST = Path(settings.BASE_DIR) / "templates"

# The host templates that are the part a shape names, or draw outside the
# frame where no part reaches, each with why.
PARTS = {
    ("403.html", "a heading"): "an error page stands outside the page frame, so it titles itself",
    ("404.html", "a heading"): "an error page stands outside the page frame, so it titles itself",
    ("500.html", "a heading"): "an error page stands outside the page frame, so it titles itself",
    ("auth/login.html", "a heading"): "the sign-in page stands outside the page frame, so it titles itself",
    ("hq_api/reference.html", "a heading"): "the API reference stands outside the page frame; its title is spoken",
    ("base.html", "a form that writes"): (
        "the frame's menu verbs, each one button, and the one form every {% post_button %} names"
    ),
    ("core/_command_center_group.html", "a <section> element"): (
        "a group of the palette's listbox: its heading labels a run of options inside one list, never a framed part"
    ),
    ("core/_command_center_group.html", "a heading"): "the palette group's label, as above",
    ("core/_command_center_results.html", "a <section> element"): (
        "the palette's search groups, the same listbox group as core/_command_center_group.html"
    ),
    ("core/_command_center_results.html", "a heading"): "the palette group's label, as above",
    ("control_plane/topology.html", "a <section> element"): (
        "the map's details pane: a labeled live region the selection moves focus to, titled by the node it shows"
    ),
    ("core/_connection_panel.html", "a fold"): "the request inspector's own disclosures, a part of its own",
    ("partials/_page_head.html", "a heading"): "the page's title, the frame's own",
    ("partials/_modal.html", "a heading"): "a dialog's title, which labels the dialog",
    ("partials/_insight_grid.html", "a heading"): "an insight's title, the part's own",
    ("partials/_problems.html", "a heading"): (
        "the urgent problems' notice, whose heading is for a screen reader alone: nothing visible titles it"
    ),
    ("partials/_form.html", "a form that writes"): "the form part itself",
    ("partials/_target_button.html", "a form that writes"): "a one-button part's own form",
    ("partials/_visit_refresh.html", "a form that writes"): "a hidden form the page's script submits, with no fields",
    ("core/_dashboard_glance.html", "a form that writes"): "hidden forms the page's script submits, with no fields",
    ("partials/_record_list.html", "a list of rows"): "the list part itself",
    ("partials/_kpi_grid.html", "a figure"): "the figure part itself",
    ("partials/_kpi_grid.html", "an icon"): "a figure's trend line, drawn by the figure part",
    ("partials/_icon.html", "an icon"): "the icon part itself",
    ("partials/_line_chart.html", "an icon"): "a chart part's own drawing",
    ("partials/_stacked_bar_chart.html", "an icon"): "a chart part's own drawing",
    ("partials/_fact.html", "a toned value"): "the fact part, toned by its projection",
    ("partials/_outside_link.html", "a toned value"): "the outside-link part, toned by its caller",
    ("partials/_activity_calendar.html", "a table outside its region"): "a month drawn as a grid, a chart part",
    ("calendars/_month_grid.html", "a table outside its region"): "a month drawn as a grid of days, not a data table",
}


def _found() -> dict[tuple[str, str], list[int]]:
    """Each host template and shape it draws by hand, with the lines."""

    found: dict[tuple[str, str], list[int]] = {}
    for path in source_files(HOST, "*.html"):
        for line, shape in hand_drawn(path.read_text(encoding="utf-8"), HAND_DRAWN):
            found.setdefault((path.relative_to(HOST).as_posix(), shape), []).append(line)
    return found


def _host() -> Counter[tuple[str, str]]:
    return Counter({key: len(lines) for key, lines in _found().items()})


@tag("source", "tree")
class HandDrawnTests(SimpleTestCase):
    def test_no_template_draws_a_shared_part_by_hand(self):
        drawn = [
            f"{name}:{line} draws {shape} by hand; write {HAND_DRAWN[shape]['write']}"
            for (name, shape), lines in sorted(_found().items())
            if (name, shape) not in PARTS and len(lines) > BASELINE.get((name, shape), 0)
            for line in lines
        ]
        self.assertEqual(drawn, [])

    def test_the_baseline_only_falls(self):
        host = _host()
        moved = {key: f"{count} -> {host[key]}" for key, count in BASELINE.items() if host[key] < count}
        self.assertEqual(moved, {}, "a page moved onto its part: lower or delete its entry in hand_drawn_baseline.py")

    def test_each_part_still_draws_its_shape(self):
        # An exception the template no longer needs leaves the list.
        host = _host()
        self.assertEqual(sorted(key for key in PARTS if not host[key]), [])

    def test_every_shape_and_entry_is_one_the_table_names(self):
        self.assertEqual({shape for _, shape in [*PARTS, *BASELINE]} - set(HAND_DRAWN), set())
        self.assertEqual(set(PARTS) & set(BASELINE), set())


# The bold runs of the host's templates at the census the count shape was read
# from: each figure set apart from the words that count it, and the bolded
# names of prose and of rows, which a count shape must leave alone.
SPLIT_COUNTS = (
    '<a href="?awaiting=1"><strong>{{ awaiting_count|breakable }}</strong> held for approval</a>',
    '<a href="?action=denied"><strong>{{ refused_count|breakable }}</strong> refused in the last day</a>',
    "<strong>{{ item.node.pageviews|breakable }}</strong> pageviews from",
    "<strong>{{ item.node.visits|breakable }}</strong> visits.",
    ", <strong>{{ finding_counts.serious|breakable }}</strong> urgent{% endif %}",
    ", <strong>{{ finding_counts.attention|breakable }}</strong> need attention{% endif %}",
    "<strong>{{ rules|length }}</strong> grants",
)
BOLD_PROSE = (
    "<p><strong>{{ name|breakable }}</strong> stops running, and whatever it serves stops answering</p>",
    "Served by <strong>{{ service.origin.headline|breakable }}</strong> from <code>{{ a }}</code>",
    "<p>Removes <strong>{{ record.title|breakable }}</strong> (<code>{{ record.doc_id }}</code>)</p>",
    "<strong>{{ hop.label|breakable }}</strong> {{ hop.check.detail|breakable }}",
    '<strong>{{ step.label|breakable }}</strong> <span class="muted">{{ step.detail }}</span>',
    "<strong>{{ switch.state|breakable }}</strong></p>",
    "<strong>{{ connection.status }}</strong> since today",
    "<strong>{{ row.address }}</strong> answers",
    '<strong>{{ finding_entries|length|counted:"problem" }}</strong>{% if one %}, urgent{% endif %}',
    '<strong>{{ item.node.pageviews|counted:"pageview" }}</strong> from',
    "Total <strong>{{ totals.total|money }}</strong>{% if narrowed %} total{% endif %}",
    "<strong>{{ account.name }}</strong> signs in",
)


@tag("source")
class SplitCountShapeTests(SimpleTestCase):
    SHAPE = "a count split from its words"

    def _drawn(self, line: str) -> bool:
        return any(shape == self.SHAPE for _, shape in hand_drawn(line, HAND_DRAWN))

    def test_each_count_set_apart_from_its_words_is_refused(self):
        self.assertEqual([line for line in SPLIT_COUNTS if not self._drawn(line)], [])

    def test_bold_names_in_prose_are_left_alone(self):
        self.assertEqual([line for line in BOLD_PROSE if self._drawn(line)], [])
