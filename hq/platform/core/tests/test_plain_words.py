"""HQ's wording rules, as Vale runs them for lint:prose, and the template text
plain_words hands Vale because Vale cannot read it."""

import contextlib
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

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
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        # Relative to the copy's root, so a section naming a directory matches.
        out = subprocess.run(
            ["vale", "--config", str(root / ".vale.ini"), "--output=JSON", *files],
            capture_output=True,
            text=True,
            check=False,
            cwd=root,
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
    def test_every_section_glob_names_a_file_type(self):
        """A glob that names directories makes Vale read bytecode and data files."""

        def expand(glob: str) -> list[str]:
            match = re.search(r"\{([^{}]*)\}", glob)
            if not match:
                return [glob]
            return [
                form
                for choice in match.group(1).split(",")
                for form in expand(glob[: match.start()] + choice + glob[match.end() :])
            ]

        headers = re.findall(r"^\[(.*)\]$", (VALE / ".vale.ini").read_text(), re.MULTILINE)
        self.assertGreater(len(headers), 1)
        for header in headers:
            if header == "formats":
                continue
            for alternative in expand(header.split(" if ")[0]):
                last = alternative.rsplit("/", 1)[-1]
                self.assertRegex(last, r"\.\w+$", f"[{header}]: {alternative} names no file type")

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

    def test_a_comment_block_is_never_read_wherever_it_stands(self):
        """After a start tag holding a template tag, and inside a link, a block
        comment is as hidden as anywhere else, and the page around it is read."""

        template = (
            '<div class="note" {% if open %}role="status"{% endif %}>Read.</div>\n'
            "{% comment %}A resource, after a start tag that holds a tag.{% endcomment %}\n"
            '<p><a href="/example/">Open {% comment %}the resource{% endcomment %}</a></p>\n'
            "{% comment %}\nA managed resource across lines.\n{% endcomment %}\n"
            "<p>A resource on the page.</p>\n"
        )

        self.assertEqual(vale({"page.html": template}), [("page.html", 7, "Resource")])

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

    def test_the_hosts_go_is_read_for_the_messages_a_page_shows(self):
        """A ProviderError's message and decodeAs's reach a finding; a log, a
        comment, another field and a test are for whoever writes the code."""

        source = (
            "package providers\n"
            "// A sweep of every provider.\n"
            "func f() error {\n"
            '\tlog.Printf("the sweep reached every provider")\n'
            '\tif a { return &ProviderError{Message: "the provider said no", Reason: "a provider reason"} }\n'
            '\tif b { return &runtime.ProviderError{Message: fmt.Sprintf("the kind %s", x), Err: err} }\n'
            '\tif c { return &ProviderError{Message: "read " + x + " in this sweep", Err: err} }\n'
            '\treturn decodeAs[T](raw, "the resource "+what+" did not decode")\n'
            "}\n"
        )

        self.assertEqual(
            vale({"controller/providers/example.go": source, "controller/providers/example_test.go": source}),
            [
                ("example.go", 5, "Provider"),
                ("example.go", 6, "Kind"),
                ("example.go", 7, "Sweep"),
                ("example.go", 8, "Resource"),
            ],
        )

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

    def test_a_sentence_past_twenty_words_is_found_however_it_is_written(self):
        """Wrapped in a template, written in pieces in Python, or run on with
        commas; a stop, a template's branch and the next string end it."""

        long = " ".join(["word"] * 21)
        page = (
            f"<p>\n  {' '.join(['word'] * 10)}\n  {' '.join(['word'] * 11)}.\n</p>\n"
            f"<p>{', '.join(['word'] * 21)}.</p>\n"
            f"<p>{' '.join(['word'] * 20)}.</p>\n"
            f"<p>{' '.join(['word'] * 12)}. {' '.join(['word'] * 12)}.</p>\n"
            f"<p>{' '.join(['word'] * 12)}{{% if x %}} {' '.join(['word'] * 12)}{{% endif %}}.</p>\n"
        )
        source = (
            f'DETAIL = ("{" ".join(["word"] * 11)} "\n          "{" ".join(["word"] * 10)}.")\n'
            f'PAIR = ("{" ".join(["word"] * 12)}",\n        "{" ".join(["word"] * 12)}")\n'
            # Capitalized: lower-case text alone is read as an identifier.
            f'ONE = "Word {long}."\n'
        )

        self.assertEqual(
            [(name, line) for name, line, rule in vale({"page.html": page, "views.py": source}) if rule == "Long"],
            [("page.html", 2), ("page.html", 5), ("views.py", 1), ("views.py", 5)],
        )

    def test_a_note_that_narrates_the_page_is_found(self):
        found = (
            "Each connection is a 1Password item the controller is given.",
            "Open one to see what to make.",
            "Press a count of links to see only what it connects.",
            "Click a row to read it.",
        )
        kept = ("Open deploy.", "Each one runs daily.", "Press Refresh after a change.", "⌘-click for more.")
        page = "".join(f"<p>{text}</p>\n" for text in (*found, *kept))

        self.assertEqual(rules({"page.html": page}), ["Narrate"] * len(found))


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


@tag("source")
class BaselineTests(SimpleTestCase):
    """A file keeps the findings its entry counts, no more, and an entry the
    whole tree no longer fills must come down."""

    LONG = "a.py:3:1: Plain.Long: too long"
    OTHER = "a.py:4:1: Plain.Sweep: a word"

    def held(self, found, *, whole=True, allowed=1):
        with mock.patch.dict(plain_words.BASELINE, {("a.py", "Plain.Long"): allowed}, clear=True):
            return plain_words.held(found, whole=whole)

    def test_the_findings_an_entry_counts_pass_and_any_other_rule_fails(self):
        self.assertEqual(self.held([self.LONG, self.OTHER]), [self.OTHER])

    def test_a_file_past_its_entry_fails_with_every_finding_named(self):
        found = self.held([self.LONG, self.LONG.replace(":3:", ":9:")])

        self.assertEqual(found[:2], [self.LONG, self.LONG.replace(":3:", ":9:")])
        self.assertIn("allows 1", found[2])

    def test_an_entry_the_tree_no_longer_fills_must_come_down(self):
        self.assertIn("lower its entry", self.held([], allowed=2)[0])
        # Part of the tree read, as a test of one template reads it, says nothing of the rest.
        self.assertEqual(self.held([], whole=False, allowed=2), [])

    def test_every_entry_names_a_file_and_a_rule_it_may_hold(self):
        from hq.platform.core.prose_baseline import BASELINE, BASELINED_RULES

        base = Path(settings.BASE_DIR)
        for (path, rule), count in BASELINE.items():
            self.assertTrue((base / path).is_file(), path)
            self.assertIn(rule, BASELINED_RULES)
            self.assertGreater(count, 0)


@tag("source", "tree")
class LabelTests(SimpleTestCase):
    """A heading, a tab, a fold, a column, a row group, a dialog, a button and
    a link to another page each say the name of the thing, never a sentence."""

    def test_a_label_that_asks_or_describes_is_refused(self):
        refused = (
            "Where it came from",
            "Actions at the default",
            "Who is allowed",
            "What",
            "Everything that happened to it",
            "Parts of this service",
            "Names HQ does not manage",
            "Each step",
            "Rules that decide it",
        )
        named = (
            "Access",
            "This week",
            "Activity",
            "Installed on",
            "Unmanaged names",
            "Request",
            "Needs you",
            "Your apps",
        )

        self.assertEqual([text for text in refused if not plain_words.label_fault(text)], [])
        self.assertEqual({text: plain_words.label_fault(text) for text in named}, dict.fromkeys(named, ""))

    def test_the_labels_are_each_label_a_template_writes(self):
        template = (
            '{% section "Where it came from" lede="What it was built from, and where." more_label="All" %}\n'
            '{% table_region "Rules" %}<table><thead><tr>{% table_heading "Who" title="Who did it, if anyone" %}'
            "</tr></thead>\n"
            '<tbody>{% row_group "Records that send traffic" n %}<tr><th scope="row">{{ name }}'
            "<p>Every call it makes is refused.</p></th></tr></tbody></table>{% endtable_region %}\n"
            '{% include "partials/_modal.html" with id="x" title="About this demo" %}\n'
            "<h2>How it runs</h2><button>Link this</button><summary>{{ n }} received but not read</summary>\n"
            '<p><a href="/a/">Everything that happened to it</a>{% dot %}<a href="/b/">Download JSON</a></p>\n'
            '<p>Read <a href="/c/">the guide for how it works</a> first.</p>\n'
            '{% outside_link url="https://example.com/" %}How it is built{% endoutside_link %}\n'
            "{% comment %}<h2>What it was</h2>{% endcomment %}\n"
        )

        self.assertEqual(
            plain_words.labels(template),
            [
                (1, "All"),
                (1, "Where it came from"),
                (2, "Rules"),
                (2, "Who"),
                (3, "Records that send traffic"),
                (4, "About this demo"),
                (5, "\x00 received but not read"),
                (5, "How it runs"),
                (5, "Link this"),
                (6, "Download JSON"),
                (6, "Everything that happened to it"),
                (8, "How it is built"),
            ],
        )

    def test_every_label_a_template_writes_is_a_name(self):
        labels = plain_words.template_labels([Path(settings.BASE_DIR) / "templates"])

        self.assertGreater(len(labels), 300)
        self.assertEqual(plain_words.label_findings(labels), [])

    def test_every_label_python_declares_is_a_name(self):
        """A page's title, a section's heading, a column, a page's action, a
        nav entry and a button's verb, wherever HQ's Python writes one."""

        base = Path(settings.BASE_DIR)
        labels = plain_words.python_labels([base / "hq", base / "hq_sdk"])

        self.assertGreater(len(labels), 200)
        self.assertEqual(plain_words.label_findings(labels), [])

    def test_a_finding_is_reported_at_its_template_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp, "page.html")
            page.write_text('<p>Fine</p>\n{% section "Who is allowed" %}{% endsection %}\n')
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                status = plain_words.main([str(page)])

        self.assertEqual(status, 1)
        self.assertEqual([line.split(": ")[0:2] for line in printed.getvalue().splitlines()], [[f"{page}:2", "Label"]])


def command_labels() -> list[tuple[str, str]]:
    """``(capability.field, label)`` for each field a command's form labels,
    its target included, as every command form and held request shows it."""

    from django import forms

    from hq.platform.application.capabilities import capability_registry
    from hq.platform.application.command_forms import command_form_class

    return [
        (f"{name}.{field_name}", str(field.label))
        for name, spec in sorted(capability_registry().items())
        for field_name, field in command_form_class(spec).base_fields.items()
        if field.label and not isinstance(field.widget, forms.HiddenInput)
    ]


@tag("source")
class DerivedLabelTests(SimpleTestCase):
    """A field with no title is labeled from its name, so a new one can show a
    word Plain refuses without any file saying it."""

    def test_every_command_field_is_labeled_in_plain_words(self):
        labels = command_labels()

        self.assertGreater(len(labels), 100)
        self.assertEqual(plain_words.refused(labels), [])

    def test_every_button_nav_entry_and_tab_hq_registers_is_a_name(self):
        """Read from the registries, so an extension's and a derived one are
        read too: a command's button, its variants, the nav and the tools."""

        from hq.platform.application.capabilities import capability_registry
        from hq.platform.application.domains import all_domains
        from hq.platform.application.toolkit import TOOL_TABS

        labels = [
            *(
                (f"{name}.{variant or 'verb'}", words)
                for name, spec in sorted(capability_registry().items())
                for variant, words in (("", spec.verb), *spec.variants)
                if words
            ),
            *((f"domain {domain.id}", domain.label) for domain in all_domains()),
            *((f"domain {domain.id} nav", item.label) for domain in all_domains() for item in domain.navigation),
            *((f"tool {tab.id}", tab.label) for tab in TOOL_TABS),
        ]

        self.assertGreater(len(labels), 40)
        self.assertEqual(plain_words.label_findings(labels), [])

    def test_a_label_derived_from_a_name_plain_refuses_is_found(self):
        from hq.platform.application.labels import field_label

        found = plain_words.refused([("example.create.kind", field_label("kind")), ("example.create.note", "Note")])

        self.assertEqual([line.split(": ")[:2] for line in found], [["example.create.kind", "Plain.Kind"]])


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
    "Apply HQ's settings",
    "Apply again",
    "Restore HQ's version",
    "Request renewal",
    "Open the deploy",
    "Sync the vault",
    "Sync documentation",
    "Topology",
    "topology",
)


class PluginLabelTests(SimpleTestCase):
    """``hq plugin check`` holds an extension's labels to the host's label rule."""

    def _checked(self, files: dict[str, str]) -> tuple[int, str]:
        from hq_sdk import validation

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(content, encoding="utf-8")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed), mock.patch("sys.argv", ["validation", tmp]):
                status = validation.main()
        return status, printed.getvalue()

    def test_a_sentence_label_in_a_template_or_python_is_refused_where_it_is(self):
        status, printed = self._checked(
            {
                "example_notes/__init__.py": "",
                "example_notes/views.py": 'from hq_sdk.pages import PageSection\n\nPageSection("x", "Every note")\n',
                "example_notes/templates/example_notes/page.html": (
                    '{% section "Who is allowed" %}{% endsection %}\n<th>Status</th>\n'
                ),
            }
        )

        self.assertEqual(status, 1)
        self.assertIn("example_notes/templates/example_notes/page.html:1: Label:", printed)
        self.assertIn("example_notes/views.py:3: Label:", printed)
        self.assertNotIn("Status", printed)

    def test_plain_labels_pass(self):
        status, printed = self._checked(
            {
                "example_notes/__init__.py": "",
                "example_notes/templates/example_notes/page.html": (
                    '{% section "Access" %}{% endsection %}\n<th>Installed on</th>\n'
                ),
            }
        )

        self.assertEqual(status, 0, printed)
