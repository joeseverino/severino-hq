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
            ("Expiry", "It expires in 3 days"),
            ("Expiry", "It expired 4 days ago"),
        )
        page = "".join(f"<p>{sentence}.</p>\n" for _rule, sentence in sentences)

        self.assertEqual(sorted(rules({"page.html": page})), sorted(rule for rule, _sentence in sentences))
        self.assertEqual(rules({"page.html": "<p>Findings</p><p>A kind-hearted reader.</p>"}), [])

    def test_an_expiry_a_template_words_around_a_value_is_found(self):
        page = '<dd>Expires in {{ days|counted:"day" }}</dd>\n<dd>{{ key.expiry|capfirst }}</dd>\n'

        self.assertEqual(vale({"page.html": page}), [("page.html", 1, "Expiry")])

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

    def test_a_sentence_past_twenty_words_runs_on_through_a_colon_or_a_semicolon(self):
        twelve = " ".join(["word"] * 12)
        page = f"<p>{twelve}: {twelve}.</p>\n<p>{twelve}; {twelve}.</p>\n<p>{twelve}. {twelve}.</p>\n"

        self.assertEqual([line for _name, line, rule in vale({"page.html": page}) if rule == "Long"], [1, 2])

    def test_a_sentence_that_stands_back_from_who_acts_is_found(self):
        found = (
            "An adoption waits for a person.",
            "The person it acts for approves it.",
            "It is the person's own device.",
            "Someone pressed Refresh.",
            "Nobody can sign in yet.",
            "Whoever asked decides it.",
            "Somebody asked for it.",
        )
        kept = ("Through a person's sign-in", "Waiting for your approval.", "An admin approves it.", "Personal token.")
        page = "".join(f"<p>{text}</p>\n" for text in (*found, *kept))

        self.assertEqual(rules({"page.html": page}), ["Person"] * len(found))

    def test_the_automations_own_parts_are_found_by_their_one_name(self):
        found = (
            "Adopted procedures",
            "One procedure runs.",
            "Open the proposal.",
            "Two proposals wait.",
            "The catalog every run is built from.",
            "The automation worker reads it.",
            "Its lease ran out.",
        )
        kept = (
            "Recovery procedure",
            "The command catalog.",
            "Its catalogs of commands.",
            "Two DHCP leases.",
            "Release.",
        )
        page = "".join(f"<p>{text}</p>\n" for text in (*found, *kept))

        self.assertEqual(rules({"page.html": page}), ["Parts"] * len(found))

    def test_what_an_agent_reads_is_held_to_the_rules_for_every_reader_alone(self):
        """A schema's, an MCP tool's and an API view's docstring is its
        description; Plain's words stand there as the API's, and any other
        docstring is for whoever writes the code."""

        source = (
            "class Answer(TypedDict):\n"
            '    """A resource held for a person."""\n'
            '@tool("Read", requires=ANYONE)\n'
            "def read():\n"
            '    """Some reads wait for someone."""\n'
            "def helper():\n"
            '    """A person writes this resource."""\n'
            "class Run(StrictCommand):\n"
            '    """Which procedure to run."""\n'
            'CapabilitySpec(wants="adopt the procedures of a commit")\n'
        )

        self.assertEqual(
            vale({"views.py": source}),
            [("views.py", 2, "Person"), ("views.py", 5, "Person"), ("views.py", 9, "Parts"), ("views.py", 10, "Parts")],
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

    def test_each_branch_of_an_attribute_is_read_alone(self):
        template = '<button aria-label="{% if open %}Show less of this row{% else %}Show all of this row{% endif %}">\n'

        self.assertEqual(plain_words.shown(template), [(1, "Show all of this row"), (1, "Show less of this row")])

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

    @tag("tree")
    def test_a_listing_names_every_finding_an_entry_counts_at_its_line(self):
        # Vale reads the text, plain_words the attribute; the file's entry counts both.
        words = " ".join(["word"] * 24)
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp, "page.html")
            page.write_text(f'<p>Fine.</p>\n<p>A {words}.</p>\n<a title="A {words}">x</a>\n')
            counted = {(str(page), "Plain.Long"): 2}

            def run(*flags):
                printed = io.StringIO()
                with mock.patch.dict(plain_words.BASELINE, counted, clear=True), contextlib.redirect_stdout(printed):
                    status = plain_words.main([*flags, str(page)])
                return status, [line.split(": ")[0:2] for line in printed.getvalue().splitlines()]

            held, listed = run(), run("--list")

        self.assertEqual(held[0], 0)
        self.assertEqual(listed, (0, [[f"{page}:2:4", "Plain.Long"], [f"{page}:3", "Plain.Long"]]))

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
                (7, "the guide for how it works"),
                (8, "How it is built"),
            ],
        )

    def test_a_link_in_a_sentence_and_a_notices_title_are_labels(self):
        template = (
            '<p>X asked to adopt it. <a href="/a/">Decide it</a> or <a href="/b/">open deploy</a>.</p>\n'
            '{% notice tone="warning" title="An adoption waits for you" %}{% endnotice %}\n'
        )

        self.assertEqual(
            plain_words.labels(template), [(1, "Decide it"), (1, "open deploy"), (2, "An adoption waits for you")]
        )

    def test_a_link_python_declares_is_a_label_and_a_commands_name_has_no_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp, "views.py")
            module.write_text(
                'Investigation("impact", "See what depends on it", "Everything that stops if this does.")\n'
                'CapabilitySpec(name="example.approve", label="Approve a held change")\n'
                'CapabilitySpec(name="example.sign_out", label="Sign it out")\n'
            )
            labels = plain_words.python_labels([Path(tmp)])
            commands = plain_words.python_labels([Path(tmp)], commands=True)

        self.assertEqual([text for _where, text in labels], ["See what depends on it"])
        self.assertEqual([text for _where, text in commands], ["Approve a held change", "Sign it out"])
        self.assertEqual(
            [finding.split(": ")[0] for finding in plain_words.label_findings(commands, most=None)], [f"{module}:3"]
        )

    def test_every_label_a_template_writes_is_a_name_past_the_baseline(self):
        base = Path(settings.BASE_DIR)
        labels = plain_words.template_labels([base / "templates"])

        self.assertGreater(len(labels), 300)
        found = plain_words.label_findings((where.removeprefix(f"{base}/"), text) for where, text in labels)
        self.assertEqual(plain_words.held(found, whole=False), [])

    def test_every_label_python_declares_is_a_name_past_the_baseline(self):
        """A page's title, a section's heading, a column, a page's action, a
        nav entry, a button's verb and a link, wherever HQ's Python writes
        one, and a command's name, of any length."""

        base = Path(settings.BASE_DIR)
        roots = [base / "hq", base / "hq_sdk"]
        labels = plain_words.python_labels(roots)
        commands = plain_words.python_labels(roots, commands=True)

        self.assertGreater(len(labels), 200)
        self.assertGreater(len(commands), 40)
        found = [
            *plain_words.label_findings((where.removeprefix(f"{base}/"), text) for where, text in labels),
            *plain_words.label_findings(
                ((where.removeprefix(f"{base}/"), text) for where, text in commands), most=None
            ),
        ]
        self.assertEqual(plain_words.held(found, whole=False), [])

    def test_a_finding_is_reported_at_its_template_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp, "page.html")
            page.write_text('<p>Fine</p>\n{% section "Who is allowed" %}{% endsection %}\n')
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                status = plain_words.main([str(page)])

        self.assertEqual(status, 1)
        self.assertEqual([line.split(": ")[0:2] for line in printed.getvalue().splitlines()], [[f"{page}:2", "Label"]])

    def test_a_label_the_baseline_counts_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp, "page.html")
            page.write_text("<p>Fine</p>\n<h2>Show them</h2>\n")
            printed = io.StringIO()
            with (
                mock.patch.dict(plain_words.BASELINE, {(str(page), "Label"): 1}, clear=True),
                contextlib.redirect_stdout(printed),
            ):
                status = plain_words.main([str(page)])

        self.assertEqual(status, 0, printed.getvalue())


@tag("source", "tree")
class KickerTests(SimpleTestCase):
    """A kicker over a heading names the group it belongs to, never a word the
    heading says again."""

    def test_a_kicker_that_repeats_its_heading_is_found_in_python_and_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "views.py").write_text(
                'Insight(eyebrow="Docs", one="doc is past its review date", many="docs are past it")\n'
                'Insight(eyebrow="GitHub", title="Failing workflows")\n'
                'Insight(eyebrow=label, title=f"{label} could not be read")\n'
                'Insight(eyebrow="Tailnet", title=f"{n} tailnet devices are offline")\n'
            )
            Path(tmp, "page.html").write_text(
                '<p class="eyebrow">Containers</p><h2>Three containers</h2>\n'
                '<p class="eyebrow">Machines</p><h2>Updates</h2>\n'
            )
            found = plain_words.kicker_findings([Path(tmp)])

        self.assertEqual(
            sorted(finding.split(": ")[0].rsplit("/", 1)[1] for finding in found),
            ["page.html:1", "views.py:1", "views.py:3", "views.py:4"],
        )
        self.assertTrue(all(finding.split(": ")[1] == "Kicker" for finding in found))

    def test_every_kicker_names_its_group_past_the_baseline(self):
        base = Path(settings.BASE_DIR)
        found = plain_words.kicker_findings([base / "templates", base / "hq", base / "hq_sdk"])

        self.assertEqual(plain_words.held([finding.removeprefix(f"{base}/") for finding in found], whole=False), [])


@tag("source", "tree")
class DumpTests(SimpleTestCase):
    def test_the_dump_lists_each_line_of_text_the_rules_read_at_its_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp, "page.html")
            page.write_text('<p>Read the deploy.</p>\n<a title="Opens the log" href="/log/">Log</a>\n{# Hidden #}\n')
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                status = plain_words.main(["--dump", str(page)])

        dumped = dict(line.split(": ", 1) for line in printed.getvalue().splitlines())
        self.assertEqual(status, 0)
        self.assertEqual(sorted(dumped), [f"{page}:1", f"{page}:2"])
        self.assertIn("Read the deploy.", dumped[f"{page}:1"])
        self.assertIn("Opens the log", dumped[f"{page}:2"])


def command_labels() -> list[tuple[str, str]]:
    """``(path:line, label)`` for each field a command's form labels, its
    target included, as every command form and held request shows it, at the
    command class it derives from, so the file's baseline entry counts it."""

    import inspect

    from django import forms

    from hq.platform.application.capabilities import capability_registry
    from hq.platform.application.command_forms import command_form_class

    base = f"{Path(settings.BASE_DIR).resolve()}/"

    def where(spec) -> str:
        path = Path(inspect.getsourcefile(spec.command_type) or spec.name).resolve()
        return f"{str(path).removeprefix(base)}:{inspect.getsourcelines(spec.command_type)[1]}"

    return [
        (where(spec), str(field.label))
        for _name, spec in sorted(capability_registry().items())
        for field in command_form_class(spec).base_fields.values()
        if field.label and not isinstance(field.widget, forms.HiddenInput)
    ]


@tag("source")
class DerivedLabelTests(SimpleTestCase):
    """A field with no title is labeled from its name, so a new one can show a
    word Plain refuses without any file saying it."""

    def test_every_command_field_is_labeled_in_plain_words(self):
        labels = command_labels()

        self.assertGreater(len(labels), 100)
        self.assertEqual(plain_words.held(plain_words.refused(labels), whole=False), [])

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
