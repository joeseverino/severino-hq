"""What a change to a stylesheet or a template reaches of the component
catalog, which fast looks at in a browser (look_reach.py)."""

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

from hq.platform.core.template_contract import parts
from hq.platform.core.tests import look_reach
from hq.platform.core.tests.browser.catalog import default_name

SHEET = """\
/* A note on the sheet, which styles nothing. */
.chart-legend { display: flex; }
.chart-legend:has(> a[href]) { line-height: var(--target-min); }
p { margin: 0; }
@media (width < 40rem) {
  .chart-legend { gap: 0; }
}
.allocation { & .chart-legend { gap: 1px; } }
.chart-legend, b { color: red; }
button:not(.chart-legend) { color: blue; }
@font-face { font-family: Example; }
"""


def _git(root: Path, *args: str) -> str:
    """git in a fixture repository, committing as an example identity past any hook the machine names."""

    identity = ["-c", "user.name=Example", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false"]
    done = subprocess.run(["git", *identity, *args], cwd=root, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _at(line: int) -> list[tuple[str, set[str] | None]]:
    return look_reach._reached(SHEET, [line])


class ChangedRuleTests(SimpleTestCase):
    def test_a_rule_reaches_what_it_names_and_one_that_names_nothing_reaches_every_entry(self):
        """A changed line reaches the classes its rule, and every rule it is
        nested in, names; a rule an alternative of which names nothing, and an
        at-rule of declarations, styles any element; a comment styles nothing."""

        self.assertEqual(_at(1), [])
        self.assertEqual(_at(3), [(".chart-legend:has(> a[href])", {"chart-legend"})])
        self.assertEqual(_at(4), [("p", None)])
        self.assertEqual(_at(5), [(".chart-legend", {"chart-legend"})])
        self.assertEqual(_at(8), [("& .chart-legend", {"allocation", "chart-legend"})])
        self.assertIsNone(_at(9)[0][1])
        self.assertIsNone(_at(10)[0][1])
        self.assertIsNone(_at(11)[0][1])


# It runs git, which the image lacks.
@tag("source")
class ReachTests(SimpleTestCase):
    def test_a_rule_a_change_deletes_reaches_the_entries_it_styled_and_no_other(self):
        """The rule is read where the base holds it, so the entries whose
        markup holds its class are reached, and an entry without it is not."""

        path = "static/css/app/figures.css"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / path).parent.mkdir(parents=True)
            _git(root, "init", "--quiet")
            (root / path).write_text(SHEET)
            _git(root, "add", "--all")
            _git(root, "commit", "--quiet", "--no-verify", "-m", "base")
            base = _git(root, "rev-parse", "HEAD")
            (root / path).write_text(
                SHEET.replace(".chart-legend:has(> a[href]) { line-height: var(--target-min); }\n", "")
            )
            with (
                mock.patch.object(look_reach, "ROOT", root),
                mock.patch.dict("os.environ", {look_reach.FAST_BASE: base}),
            ):
                reach = look_reach.reach(path)

        drawn = look_reach.drawings()
        self.assertIn("part/allocation-legend", reach.entries)
        self.assertTrue(all("chart-legend" in drawn[entry].words for entry in reach.entries))
        self.assertLess(len(reach.entries), len(drawn))

    def test_a_change_to_a_parts_template_reaches_an_entry_that_draws_it(self):
        """A part's partial is judged on the part's own entry, which draws its
        example, and each piece of it on an entry that draws that piece, so no
        file of a shared part is looked at on nothing."""

        for part in parts():
            for at, file in enumerate(part.files()):
                with self.subTest(file):
                    reached = look_reach.reach(f"templates/{file}").entries
                    self.assertTrue(reached)
                    if not at:
                        self.assertIn(default_name(part), reached)
