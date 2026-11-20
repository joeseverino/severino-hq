"""The style bundle as the contract extensions render against."""

import re
import subprocess
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.core.stylesheets import STATIC, sheet_paths, stylesheet
from hq.platform.core.table_cells import ROLE_CLASSES
from hq.platform.core.template_contract import class_words

from .hand_rolled import hand_rolled_classes, needless_classes
from .source import template_files


def _is_address(text: str, found: re.Match[str]) -> bool:
    """Whether a dotted quad in text is an address, not a version or an RFC section."""

    if any(int(part) > 255 for part in found.group().split(".")):
        return False
    return text[max(0, found.start() - 8) : found.start()] != "section-"


# Documentation, private, multicast and reserved ranges are examples or
# classes, not places. Anything outside them is somewhere a packet can
# actually go.
#
# 192.168.0.0/16 is the exception: a host from it is somebody's actual
# machine rather than an example. Only the range's base address passes,
# since that appears solely as half of a CIDR. Fixtures wanting a
# private address use 10.0.0.0/8, which every classifier here treats
# identically.
_RESERVED = re.compile(
    r"^(?:127\.|10\.|192\.168\.0\.0$|169\.254\.|0\.|255\.|22[4-9]\.|2[3-5][0-9]\.|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])\.|"
    r"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|"
    r"203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|192\.0\.0\.|"
    r"1\.1\.1\.1|8\.8\.8\.8)"
)
_ADDRESS = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
# Long enough to be key material rather than a fixture. A real
# ed25519 public key is 68 base64 characters; tests legitimately use
# short stand-ins, and failing on those would teach people to weaken
# the check rather than fix a leak.
_HOST_KEY = re.compile(r"ssh-(?:ed25519|rsa) AAAA[A-Za-z0-9+/]{32,}")
# A hostname under the deployment's own private zone. Not secret
# (nothing outside the network resolves it) but it names one
# installation's topology, and a public repository holds the shape
# rather than the deployment. `example` and `invalid` are reserved for
# writing about hostnames, which is what a fixture is doing.
_PRIVATE_HOST = re.compile(
    r"\b[a-z0-9-]+\.(?!example\b|invalid\b|test\b|localhost\b)"
    r"(?:homelab|lan|internal|local)\b(?!\.(?:example\.)?toml\b)"
)
# Lockfiles and pinned action SHAs are hashes, not hosts. Vendored
# bundles/specs carry upstream examples, pinned by their UPSTREAM.
_SKIP = (
    "package-lock.json",
    "uv.lock",
    ".github/",
    "controller/api/vendor/",
    "static/vendor/",
)


def _pushable_files(root: Path) -> list[str]:
    """Every file a push would carry: tracked, and new but not ignored, so a
    file is checked by the commit that first adds it rather than the one after."""

    return subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")


def _deployment_terms(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The addresses the deployment publishes on purpose, and the terms no file may name.

    Names a checkout's own deployment goes by, one per line, kept in
    .git/info where nothing is tracked or pushed. A line starting with `!` is
    an address the deployment publishes on purpose (a public demo), which a
    file may name although a term covers it. Absent, only the generic
    patterns apply.
    """

    terms_file = Path(
        subprocess.run(
            ["git", "rev-parse", "--git-path", "info/deployment-terms"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    if not terms_file.is_absolute():
        terms_file = root / terms_file
    try:
        lines = [
            line.strip().lower()
            for line in terms_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    except OSError:
        lines = []
    published = tuple(line[1:] for line in lines if line.startswith("!"))
    return published, tuple(line for line in lines if not line.startswith("!"))


def _endpoints_in(text: str, published: tuple[str, ...], terms: tuple[str, ...]) -> list[str]:
    """Each reachable endpoint ``text`` names: an address, a host key, a private host or a deployment term."""

    found = [
        match.group()
        for match in _ADDRESS.finditer(text)
        if _is_address(text, match) and not _RESERVED.match(match.group())
    ]
    if _HOST_KEY.search(text):
        found.append("ssh host key")
    found.extend(sorted(set(_PRIVATE_HOST.findall(text))))
    lowered = text.lower()
    for address_published in published:
        lowered = lowered.replace(address_published, "")
    found.extend(f"deployment term #{index + 1}" for index, term in enumerate(terms) if term in lowered)
    return found


_TABLE_PARTS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th"})
_BOXES = frozenset({"block", "flex", "grid", "inline-flex", "inline-grid"})


def _table_part_classes() -> set[str]:
    """Every class the templates put on a table part."""

    cell_classes: set[str] = set()
    for _name, template in template_files():
        for attrs in re.findall(
            r"<(?:table|thead|tbody|tfoot|tr|td|th)\b([^>]*)>",
            template.read_text(encoding="utf-8"),
        ):
            found = re.search(r'class="([^"{}]*)"', attrs)
            if found:
                cell_classes.update(found.group(1).split())
    return cell_classes


def _styles_a_table_part(selector: str, display: str, cell_classes: set[str]) -> bool:
    """Whether one selector of a rule declaring ``display`` styles a table part itself."""

    # The subject is the rightmost compound selector: what the rule
    # actually styles, rather than what it is scoped by.
    subject = selector.rsplit(maxsplit=1)[-1].rsplit(">", maxsplit=1)[-1].strip()
    # A part's pseudo-element is a box inside the part, not the part:
    # giving it a display leaves the cell in table layout.
    if "::" in subject:
        return False
    # An element qualifier settles it either way: `span.x` cannot
    # match a table part however `.x` is used elsewhere, and `td.x`
    # always does. Only an unqualified class has to be judged by
    # where the templates put it.
    qualifier = re.match(r"^([A-Za-z][\w-]*)", subject)
    if qualifier:
        return qualifier.group(1) in _TABLE_PARTS
    # `block` is common on spans that share a class with a cell
    # (`.muted`), so it is judged only where a table part is named.
    if display == "block":
        return False
    return bool(set(re.findall(r"\.([A-Za-z0-9_-]+)", subject)) & cell_classes)


@tag("source", "tree")
class StyleContractTests(SimpleTestCase):
    """The style bundle is a contract that extensions render against.

    These guard failures that are invisible in review and silent at runtime:
    CSS resolves an undefined custom property to an invalid value rather than
    erroring, so a typo'd token degrades to a browser default (a black SVG
    fill) instead of breaking loudly.
    """

    @staticmethod
    def _stylesheet() -> str:
        return stylesheet()

    def test_a_blur_has_something_to_show_through(self):
        """A backdrop filter costs a layer redrawn on every scroll; behind an
        opaque background it shows nothing, so a rule that blurs must also let
        the page through (`transparent` mixed in, or a color with alpha)."""

        for body in re.findall(r"\{([^{}]*backdrop-filter[^{}]*)\}", self._stylesheet()):
            background = re.search(r"background(?:-color)?:\s*([^;]+);", body)
            with self.subTest(rule=body.strip()[:80]):
                self.assertIsNotNone(background)
                self.assertRegex(background.group(1), r"transparent|/\s*[.\d]+\)|rgba|hsla")

    def test_a_table_part_is_never_given_a_block_flex_or_grid_display(self):
        """A table is laid out by its primitive alone (tables.css, a grid whose
        rows are its subgrids), so a box display any other sheet gives a table
        part takes the row out of its tracks: its cells no longer line up with
        the columns, and neither fails loudly. This reads the classes the
        templates actually put on table parts and rejects the declaration
        there. Lay out a div inside the cell instead.
        """

        cell_classes = _table_part_classes()
        offenses = []
        # Comments first: one sitting above a rule is otherwise read as part of
        # that rule's selector.
        sheets = "\n".join(path.read_text(encoding="utf-8") for path in sheet_paths() if path.name != "tables.css")
        source = re.sub(r"/\*.*?\*/", " ", sheets, flags=re.DOTALL)
        for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
            declared = re.search(r"(?<![\w-])display\s*:\s*([a-z-]+)", block)
            if not declared or declared.group(1) not in _BOXES:
                continue
            offenses.extend(
                f"{part.strip()} sets display: {declared.group(1)}"
                for part in selector.split(",")
                if part.strip() and _styles_a_table_part(part.strip(), declared.group(1), cell_classes)
            )

        self.assertEqual(sorted(set(offenses)), [])

    def test_a_rows_block_detail_opens_beneath_the_row(self):
        """`row-more` shows a part in its own cell once the row is open, which
        suits a word or a list item. A block (a list, a paragraph, a panel)
        shown there stacks the whole detail into one column beside empty ones,
        so it is `row-below`, the line beneath the row at the table's width."""

        offenses = [
            f"{name}: <{tag}>"
            for name, template in template_files()
            for tag in re.findall(
                r'<(div|ul|ol|p|section|form|table|dl)\b[^>]*class="[^"]*\brow-more\b',
                template.read_text(encoding="utf-8"),
            )
        ]
        self.assertEqual(offenses, [])

    # Viewport breakpoints left, each for something the viewport genuinely
    # decides: the header and nav, a modal that takes the whole screen, table
    # sizing, the head's overflow menu, the dashboard strip, a few dense
    # diagrams. It only goes down. A layout answers to its own width with a
    # fluid rule (auto-fit, flex-wrap, clamp) or a container query instead.
    VIEWPORT_BREAKPOINTS = 10

    def test_viewport_breakpoints_only_go_down(self):

        css = re.sub(r"/\*.*?\*/", " ", self._stylesheet(), flags=re.DOTALL)
        found = [q for q in re.findall(r"@media[^{]*\{", css) if "width" in q]
        self.assertLessEqual(
            len(found),
            self.VIEWPORT_BREAKPOINTS,
            "A new viewport breakpoint: make the layout fluid or ask its container "
            "(docs/Design.md, 'Size to content, fit the window').",
        )

    def test_a_selector_is_styled_in_one_place(self):
        """A component's rule lives once, so a change to it is made once.

        Only rules naming a single selector are counted: a shared list (the
        frame rule, the band rule) and a state beside its base (`a, a:hover`)
        are the idioms that put one name in two rules on purpose. Breakpoint
        and container variants are the same rule at another size and are not
        counted either.

        Stylelint's no-duplicate-selectors does not replace it: it judges one
        file and one at-rule block at a time, so a selector in two sheets or
        in two `@layer` blocks of one sheet passes it.
        """

        import collections

        css = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), self._stylesheet(), flags=re.DOTALL)
        seen: dict[str, list[int]] = collections.defaultdict(list)
        stack: list[str] = []
        start = 0
        for match in re.finditer(r"[{}]", css):
            if match.group() == "{":
                selector = " ".join(css[start : match.start()].split())
                stack.append(selector)
                outer = stack[:-1]
                top_level = all(s.startswith("@layer") for s in outer)
                single = "," not in re.sub(r"\([^()]*\)", "", selector)
                if top_level and single and not selector.startswith("@"):
                    seen[selector].append(css.count("\n", 0, match.start()) + 1)
            elif stack:
                stack.pop()
            start = match.end()
        twice = {selector: lines for selector, lines in seen.items() if len(lines) > 1}
        self.assertEqual(twice, {})

    def test_a_fallback_never_hides_an_undefined_custom_property(self):
        """A var() with a fallback still names a property some sheet defines.

        Stylelint's no-unknown-custom-properties (`lint:css`) passes any
        var() that carries a fallback, and a typo there degrades silently to
        the fallback.
        """

        css = "\n".join(path.read_text(encoding="utf-8") for path in sorted((STATIC / "css").rglob("*.css")))
        defined = set(re.findall(r"(--[\w-]+)\s*:", css))
        fallen_back = set(re.findall(r"var\(\s*(--[\w-]+)\s*,", css))
        self.assertTrue(fallen_back)
        self.assertEqual(sorted(fallen_back - defined), [])

    def test_no_tracked_file_names_a_reachable_endpoint(self):
        """Addresses, ports and account names are deployment facts, not source.

        This repository is public, so an endpoint committed here is published
        whether or not anything treats it as a secret. Deployment facts belong
        in 1Password and reach the controller through the env the connection
        registry already renders; what stays here is the shape.

        Asked of git rather than the filesystem, because the question is what
        would be pushed. A working tree holds plenty that is nobody's business
        and is correctly ignored.
        """

        root = Path(__file__).resolve().parents[4]
        published, terms = _deployment_terms(root)
        findings = []
        for name in _pushable_files(root):
            if not name or any(part in name for part in _SKIP):
                continue
            try:
                text = (root / name).read_text(encoding="utf-8")
            except OSError, UnicodeDecodeError:
                continue
            findings.extend(f"{name}: {finding}" for finding in _endpoints_in(text, published, terms))

        self.assertEqual(
            findings,
            [],
            f"reachable endpoints in tracked files: {findings}. A dotted number that is not an address,"
            " such as a specification's section, passes written as section-3.1.3.7. A host key a fixture"
            " needs passes written short, under 32 base64 characters after AAAA.",
        )

    def test_chart_templates_do_not_hardcode_the_plot_rectangle(self):
        """The geometry is in ui.py, and a copy of it in a template is a bug.

        A template that spells out plot coordinates stays behind when the plot
        moves: the bars and the line move, its gridlines and marks do not. Any
        literal that equals a plot coordinate is that fault.
        """

        from ..ui import PLOT_HEIGHT, PLOT_LEFT, PLOT_TOP, PLOT_WIDTH

        forbidden = {
            str(round(value))
            for value in (
                PLOT_LEFT,
                PLOT_TOP,
                PLOT_LEFT + PLOT_WIDTH,
                PLOT_TOP + PLOT_HEIGHT,
            )
        }
        root = Path(__file__).resolve().parents[4]
        offenders = []
        for template in (root / "templates" / "partials").glob("*chart*.html"):
            text = template.read_text(encoding="utf-8")
            # Only the SVG geometry attributes. A viewBox is rendered from the
            # chart's own width and height, so it is read from the model too.
            for attribute, literal in re.findall(r'\b(x1|x2|y1|y2|cx|cy|width|height)="([0-9.]+)"', text):
                if literal.split(".")[0] in forbidden:
                    offenders.append(f'{template.name}: {attribute}="{literal}"')
        self.assertEqual(
            offenders,
            [],
            "chart templates must read plot coordinates from the chart, not restate them",
        )

    def test_categorical_series_slots_are_defined_and_distinct(self):

        css = self._stylesheet()
        slots = re.findall(
            r"^\s*(--series-\d+)\s*:\s*light-dark\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)",
            css,
            re.MULTILINE,
        )
        self.assertGreaterEqual(len(slots), 5, "expected at least 5 categorical slots")
        for theme, index in (("light", 1), ("dark", 2)):
            values = [slot[index].lower() for slot in slots]
            with self.subTest(theme=theme):
                self.assertEqual(len(values), len(set(values)), "series slots must be distinct")


def defined_classes() -> set[str]:
    """Every class a stylesheet HQ serves defines: the bundle every page
    links, and a sheet one page links on its own."""

    sheets = (path.read_text(encoding="utf-8") for path in (STATIC / "css").rglob("*.css"))
    return {name for sheet in sheets for name in re.findall(r"\.([a-z][a-z0-9-]*)", sheet)}


@tag("source", "tree")
class TemplateClassStyleTests(SimpleTestCase):
    """Every class a template names is one the stylesheet defines.

    A class the bundle lacks raises no error; the element is unstyled,
    so a page that means a primitive and misspells it, or keeps a class whose
    rule is gone, looks wrong with nothing to say why. Read over every
    template Django loads, an extension's included, since extensions draw
    with the host's stylesheet.
    """

    maxDiff = None

    def test_template_classes_are_defined_in_the_stylesheet(self):

        # A class that declares a part's role in a table is read by the region,
        # which marks the part for the stylesheet (table_tags.mark_cells).
        defined = defined_classes() | set(ROLE_CLASSES)
        offenders = sorted(
            f"{name}: .{word}"
            for name, path in template_files()
            for _, word in class_words(path.read_text(encoding="utf-8"))
            if not word.endswith("*") and word not in defined
        )
        self.assertEqual(offenders, [], "define the class where its part is styled, or delete it")

    def test_each_dialog_size_is_one_the_dialog_offers(self):
        """The dialog draws its `size` as a class built as it renders, which the
        literal read above never sees, so a size it does not offer would draw
        a form-width dialog with nothing to say why."""

        defined = defined_classes()
        sizes = [
            (name, size)
            for name, path in template_files()
            for include in re.findall(
                r'{%\s*include\s+"partials/_modal\.html"[^%]*%}', path.read_text(encoding="utf-8")
            )
            for size in re.findall(r'\bsize="([^"]*)"', include)
        ]
        self.assertTrue(sizes, "no dialog names a size, so this check reads nothing")
        self.assertEqual(
            [f"{name}: size={size!r}" for name, size in sizes if f"modal-{size}" not in defined],
            [],
            "name a size the dialog offers (partials/_modal.html), or add it beside .modal-wide in forms.css",
        )


@tag("source", "tree")
class HandRolledClassTests(SimpleTestCase):
    """A class is one part's own, declared on its ``Part``: a template writes
    only the public classes, as an extension does, and a part's partial writes
    besides them its own and those of the parts it draws."""

    def test_host_templates_draw_only_the_classes_they_may(self):
        """A class a template writes that no part lets it is a hand-rolled part
        the survey never sees and an extension cannot draw."""

        self.assertEqual(
            sorted(f"templates/{name}: .{word}" for name, word in hand_rolled_classes()),
            [],
            "a page writes only the public classes (template_contract.public_classes), as an extension does: draw "
            "it through the part that owns it, or grow that part an option; a part's partial writes only the classes "
            "its Part declares, or those of a part it draws",
        )

    def test_a_part_declares_only_the_classes_its_partial_writes(self):
        self.assertEqual(sorted(needless_classes()), [], "remove it from the Part's classes")
