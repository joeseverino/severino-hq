"""A titled part of a host page is drawn by ``{% section %}`` (docs/Design.md,
"A section is drawn by {% section %}"), so its head, frame and spacing are
the part's and a page states only its content.

What draws one by hand: a ``<section>`` element, ``{% section_head %}``, a
``{% card %}`` or a fold's summary whose first content is a heading. A
``<section>`` that only groups a layout is a plain element. Only the host's
own templates are read: an extension moves on its own schedule.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from .source import source_files

# Each shape a hand-drawn titled section takes, by what it is called in a failure.
BY_HAND = {
    "a <section> element": re.compile(r"<section\b"),
    "{% section_head %}": re.compile(r"\{%\s*section_head\b"),
    "a card titled by its heading": re.compile(r'\{%\s*card\b(?:(?!%\}|element="figure").)*%\}\s*<h[1-6]\b', re.DOTALL),
    "a fold titled by its heading": re.compile(r"<summary\b[^>]*>\s*<h[1-6]\b"),
}

# The ones whose structure is not a page's titled part, each with why.
ALLOWED = {
    ("core/_command_center_group.html", "a <section> element"): (
        "a group of the palette's listbox: its heading labels a run of options inside one list, never a framed part"
    ),
    ("core/_command_center_results.html", "a <section> element"): (
        "the palette's search groups, the same listbox group as core/_command_center_group.html"
    ),
    ("control_plane/topology.html", "a <section> element"): (
        "the map's details pane: a labeled live region the selection moves focus to, titled by the node it shows"
    ),
    ("partials/_problems.html", "a card titled by its heading"): (
        "the urgent problems' notice, whose heading is for a screen reader alone: nothing visible titles it"
    ),
}

_COMMENT = re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}", re.DOTALL)


def _drawn_by_hand(root: Path) -> dict[tuple[str, str], list[int]]:
    """Each template under ``root`` and shape it draws by hand, with the lines."""

    found: dict[tuple[str, str], list[int]] = {}
    for path in source_files(root, "*.html"):
        text = _COMMENT.sub(lambda match: "\n" * match.group().count("\n"), path.read_text(encoding="utf-8"))
        for shape, pattern in BY_HAND.items():
            for match in pattern.finditer(text):
                key = (path.relative_to(root).as_posix(), shape)
                found.setdefault(key, []).append(text.count("\n", 0, match.start()) + 1)
    return found


@tag("source", "tree")
class TitledSectionTests(SimpleTestCase):
    ROOT = Path(settings.BASE_DIR) / "templates"

    def test_no_host_template_draws_a_titled_section_by_hand(self):
        found = _drawn_by_hand(self.ROOT)
        drawn = [
            f"{name}:{line} {shape}"
            for (name, shape), lines in sorted(found.items())
            if (name, shape) not in ALLOWED
            for line in lines
        ]
        self.assertEqual(
            drawn,
            [],
            'write {% section "Heading" %}body{% endsection %}, or a plain element for one that only groups a layout',
        )

    def test_each_allowed_shape_is_still_drawn(self):
        # An exception the template does not need leaves the list.
        found = _drawn_by_hand(self.ROOT)
        self.assertEqual(sorted(key for key in ALLOWED if key not in found), [])
