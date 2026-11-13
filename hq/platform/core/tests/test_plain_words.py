"""HQ's wording rules, as Vale runs them for lint:prose, and the template text
plain_words hands Vale because Vale cannot read it."""

import contextlib
import io
import json
import re
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
            "{% comment %}A sweep, after a start tag that holds a tag.{% endcomment %}\n"
            '<p><a href="/example/">Open {% comment %}the sweep{% endcomment %}</a></p>\n'
            "{% comment %}\nA sweep across lines.\n{% endcomment %}\n"
            "<p>A sweep on the page.</p>\n"
        )

        self.assertEqual(vale({"page.html": template}), [("page.html", 7, "Sweep")])

    def test_every_form_of_every_word_is_found_in_a_sentence(self):
        sentences = (
            ("Sweep", "One sweep"),
            ("Sweep", "Two sweeps"),
            ("Sweep", "Swept today"),
            ("Sweep", "Sweeping now"),
            ("Spec", "Its spec"),
            ("Spec", "Its specs"),
            ("Mint", "Mint one"),
            ("Mint", "It mints one"),
            ("Mint", "Minted today"),
            ("Mint", "Minting now"),
            ("Expiry", "It expires in 3 days"),
            ("Expiry", "It expired 4 days ago"),
        )
        page = "".join(f"<p>{sentence}.</p>\n" for _rule, sentence in sentences)

        self.assertEqual(sorted(rules({"page.html": page})), sorted(rule for rule, _sentence in sentences))
        self.assertEqual(rules({"page.html": "<p>A sweeper.</p><p>Spectacle.</p><p>Spearmint.</p>"}), [])

    def test_an_expiry_a_template_words_around_a_value_is_found(self):
        page = '<dd>Expires in {{ days|counted:"day" }}</dd>\n<dd>{{ key.expiry|capfirst }}</dd>\n'

        self.assertEqual(vale({"page.html": page}), [("page.html", 1, "Expiry")])

    def test_a_retired_name_is_found_as_written(self):
        page = "".join(f"<p>{name}</p>\n" for name in RETIRED)

        self.assertEqual(rules({"page.html": page}).count("Retired"), len(RETIRED))
        self.assertEqual(rules({"page.html": "<p>3 things need you. Refresh all.</p>"}), [])

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
            'help = ("Write it. " "--check exits 1 on its spec.")\n'
        )

        self.assertEqual(vale({"views.py": source}), [("views.py", 8, "Sweep"), ("views.py", 9, "Spec")])

    def test_the_hosts_go_is_read_for_the_messages_a_page_shows(self):
        """A ProviderError's message and decodeAs's reach a finding; a log, a
        comment, another field and a test are for whoever writes the code."""

        source = (
            "package providers\n"
            "// A sweep of every provider.\n"
            "func f() error {\n"
            '\tlog.Printf("the sweep reached every provider")\n'
            '\tif a { return &ProviderError{Message: "the spec said no", Reason: "a spec reason"} }\n'
            '\tif b { return &runtime.ProviderError{Message: fmt.Sprintf("the sweep %s", x), Err: err} }\n'
            '\tif c { return &ProviderError{Message: "read " + x + " in this sweep", Err: err} }\n'
            '\treturn decodeAs[T](raw, "the spec "+what+" did not decode")\n'
            "}\n"
        )

        self.assertEqual(
            vale({"controller/providers/example.go": source, "controller/providers/example_test.go": source}),
            [
                ("example.go", 5, "Spec"),
                ("example.go", 6, "Sweep"),
                ("example.go", 7, "Sweep"),
                ("example.go", 8, "Spec"),
            ],
        )

    def test_an_em_dash_is_found_in_any_file(self):
        dash = chr(0x2014)
        files = {
            "page.html": f"<p>one {dash} two</p>\n",
            "views.py": f'LABEL = "one {dash} two"\n',
            "notes.md": f"# Notes\n\nOne {dash} two.\n",
        }

        self.assertEqual(rules(files), ["Dash"] * 3)

    def test_the_automations_own_parts_are_found_by_their_one_name(self):
        found = (
            "Adopted procedures",
            "One procedure runs.",
            "Open the proposal.",
            "Two proposals wait.",
            "The catalog every run is built from.",
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

    def test_what_an_agent_reads_is_held_to_the_automations_names(self):
        """A schema's, an MCP tool's and an API view's docstring is its
        description, where the automation's parts take their one name; any
        other docstring is for whoever writes the code."""

        source = (
            "class Answer(TypedDict):\n"
            '    """A procedure held for review."""\n'
            '@tool("Read", requires=ANYONE)\n'
            "def read():\n"
            '    """Some reads wait for a procedure."""\n'
            "def helper():\n"
            '    """A helper writes this procedure."""\n'
            "class Run(StrictCommand):\n"
            '    """Which procedure to run."""\n'
            'CapabilitySpec(wants="adopt the procedures of a commit")\n'
        )

        self.assertEqual(
            vale({"views.py": source}),
            [("views.py", 2, "Parts"), ("views.py", 5, "Parts"), ("views.py", 9, "Parts"), ("views.py", 10, "Parts")],
        )


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
            page.write_text('<p>Fine</p>\n\n<a title="Read the deploy">x</a>\n<a title="Start the sweep">y</a>\n')
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                status = plain_words.main([str(page)])

        self.assertEqual(status, 1)
        self.assertEqual(
            [line.split(": ")[0:2] for line in printed.getvalue().splitlines()], [[f"{page}:4", "Plain.Sweep"]]
        )

    def test_each_branch_of_an_attribute_is_read_alone(self):
        template = '<button aria-label="{% if open %}Show less of this row{% else %}Show all of this row{% endif %}">\n'

        self.assertEqual(plain_words.shown(template), [(1, "Show all of this row"), (1, "Show less of this row")])

    def test_a_path_that_is_gone_fails_rather_than_passing(self):
        with self.assertRaises(FileNotFoundError):
            plain_words.document([Path("templates/no-such-page.html")])


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
    command class it derives from."""

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
        self.assertEqual(plain_words.refused(labels), [])

    def test_a_label_derived_from_a_name_plain_refuses_is_found(self):
        from hq.platform.application.labels import field_label

        found = plain_words.refused([("example.create.spec", field_label("spec")), ("example.create.note", "Note")])

        self.assertEqual([line.split(": ")[:2] for line in found], [["example.create.spec", "Plain.Spec"]])


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
