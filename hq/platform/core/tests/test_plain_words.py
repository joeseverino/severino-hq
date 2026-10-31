"""HQ's wording rules, as Vale runs them for lint:prose, and the template text
plain_words hands Vale because Vale cannot read it."""

import contextlib
import io
import json
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.platform.core import plain_words

# Named as a path, so the fast gate runs this test when the config changes.
VALE = Path(settings.BASE_DIR).resolve() / "tools/lint/vale"
# Every rule applies to a scratch module, which no path section of the config
# names: the later section holds.
EVERY_RULE = "\n[*.py]\nBasedOnStyles = HQ, Plain\n"


def vale(files: dict[str, str]) -> list[tuple[str, int, str]]:
    """``(file, line, rule)`` for each finding Vale makes in ``files``."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Vale reads StylesPath from the copy's own directory.
        config = (VALE / ".vale.ini").read_text().replace("StylesPath = styles", f"StylesPath = {VALE / 'styles'}")
        (root / ".vale.ini").write_text(config + EVERY_RULE)
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        out = subprocess.run(
            ["vale", "--config", str(root / ".vale.ini"), "--output=JSON", *(str(root / name) for name in files)],
            capture_output=True,
            text=True,
            check=False,
        )
        alerts = json.loads(out.stdout or "{}")
    return sorted(
        (Path(path).name, alert["Line"], alert["Check"].split(".")[1])
        for path, found in alerts.items()
        for alert in found
    )


def rules(files: dict[str, str]) -> list[str]:
    return [rule for _name, _line, rule in vale(files)]


@tag("source")
class ValeRuleTests(SimpleTestCase):
    def test_a_template_is_read_for_its_text_and_not_its_tags_or_comments(self):
        template = (
            "{% comment %}The sweep reconciles a kind.{% endcomment %}\n"
            '<a class="sweep-link" href="{% url \'control_plane:reconcile\' %}">Open</a>\n'
            "{# a sweep of every kind #}\n"
            '<li class="tone-{{ standing.state }}"{% if standing.met %} title="x"{% endif %}>Read 3 records.</li>\n'
            "<code>adguard.rewrite</code><p>Findings</p>\n"
            "<p>The sweep ran.</p>\n"
        )

        self.assertEqual(vale({"page.html": template}), [("page.html", 6, "Sweep")])

    def test_every_form_of_every_word_is_found_in_a_sentence(self):
        sentences = (
            ("Kind", "Pick a kind"),
            ("Sweep", "Swept today"),
            ("Reconcile", "Reconciling now"),
            ("Reconcile", "It converged"),
            ("Declared", "The desired state"),
            ("Spec", "Its specs"),
            ("Observed", "An observation"),
            ("Generation", "Each revision"),
            ("Drift", "It drifted"),
            ("Provider", "Two providers"),
            ("Resource", "A managed resource"),
            ("Finding", "Each finding"),
            ("Finding", "Two findings"),
            ("Claim", "It raised one"),
            ("Internal", "Its standing"),
            ("Capability", "One capability"),
            ("Consumer", "Every consumer"),
            ("Render", "It rendered"),
            ("Mint", "Minted today"),
        )
        page = "".join(f"<p>{sentence}.</p>\n" for _rule, sentence in sentences)

        self.assertEqual(sorted(rules({"page.html": page})), sorted(rule for rule, _sentence in sentences))
        self.assertEqual(rules({"page.html": "<p>Findings</p><p>A kind-hearted reader.</p>"}), [])

    def test_a_retired_name_is_found_as_written(self):
        page = "".join(f"<p>{name}</p>\n" for name in RETIRED)

        self.assertEqual(rules({"page.html": page}).count("Retired"), len(RETIRED))
        self.assertEqual(rules({"page.html": "<p>3 things need you. Refresh all.</p>"}), [])

    def test_a_setting_name_or_a_field_value_is_found_even_in_code(self):
        page = (
            "<p>Set <code>SEVERINO_EXAMPLE_SWITCH</code> to allow it.</p>\n"
            "<p>Set manages to 1 on its item.</p><code>example.rewrite</code>\n"
        )

        self.assertEqual(rules({"page.html": page}), ["Asked", "Asked"])

    def test_python_is_read_for_sentences_and_not_for_names(self):
        source = (
            '"""A sweep of every kind."""\n'
            "# A sweep of every kind.\n"
            'KIND = "adguard.rewrite"\n'
            'route = "control_plane:reconcile"\n'
            'logger.info("The sweep reached every provider.")\n'
            'raise ImproperlyConfigured("The sweep " "reached every provider.")\n'
            'PDF = b"/Resources<</Font 1>>"\n'
            'title = "Was not in the last sweep"\n'
            'help = ("Write it. " "--check exits 1 on drift.")\n'
        )

        self.assertEqual(vale({"views.py": source}), [("views.py", 8, "Sweep"), ("views.py", 9, "Drift")])

    def test_narration_is_found_in_any_file(self):
        files = {
            "views.py": "x = 1  # This turned out to be wrong.\n",
            "page.js": "// The first version read it twice.\n",
            "page.html": "{% comment %}We tried a timer.{% endcomment %}\n",
            "notes.md": "# Notes\n\nIt used to be a list.\n",
        }

        self.assertEqual(rules(files), ["History"] * 4)

    def test_an_em_dash_and_a_hedged_plural_are_found(self):
        dash = chr(0x2014)
        files = {
            "page.html": f"<p>one {dash} two</p>\n<p>{{{{ n }}}} account(s), over http(s)</p>\n",
            "views.py": f'LABEL = "one {dash} two"\nROWS = f"{{n}} row(s)"\n',
        }

        self.assertEqual(sorted(rules(files)), ["Dash", "Dash", "Hedge", "Hedge"])


@tag("source", "tree")
class TemplatePieceTests(SimpleTestCase):
    def test_the_pieces_are_attributes_and_tag_literals_a_person_reads(self):
        template = (
            '{% comment %}title="The sweep"{% endcomment %}\n'
            '<a class="sweep-link" href="{% url \'control_plane:reconcile\' %}" title="Reconcile it">Open</a>\n'
            '{% include "partials/_sweep_freshness.html" with message="No resources yet." %}\n'
            '<img alt="A chart"><input placeholder="lower case only" data-tip="Says why">\n'
            "<script>const title = 'Not shown';</script>\n"
        )

        self.assertEqual(
            plain_words.shown(template),
            [(2, "Reconcile it"), (3, "No resources yet."), (4, "A chart"), (4, "Says why"), (4, "lower case only")],
        )

    def test_a_finding_is_reported_at_its_template_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp, "page.html")
            page.write_text('<p>Fine</p>\n\n<a title="Read the deploy">x</a>\n<a title="Reconcile it">y</a>\n')
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                status = plain_words.main([str(page)])

        self.assertEqual(status, 1)
        self.assertEqual(
            [line.split(": ")[0:2] for line in printed.getvalue().splitlines()], [[f"{page}:4", "Plain.Reconcile"]]
        )

    def test_a_path_that_is_gone_fails_rather_than_passing(self):
        with self.assertRaises(FileNotFoundError):
            plain_words.document([Path("templates/no-such-page.html")])


RETIRED = (
    "Action items",
    "Command Center",
    "Command center",
    "Request fresh sweep",
    "Read now and check again",
    "Read now",
    "Read them now",
    "Read all now",
    "Read everything now",
    "Refresh now",
    "Reading…",
    "Reading failed",
    "Refresh controller readings",
    "Run updates now",
    "Run dependency updates now",
    "Run the controller",
    "Run a pass now",
    "Apply again",
    "Restore HQ's version",
    "Request renewal",
    "Open the deploy",
    "Sync the vault",
    "Sync documentation",
)
