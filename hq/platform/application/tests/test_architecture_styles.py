"""The style bundle as the contract extensions render against."""

import re
import subprocess
from pathlib import Path

from django.test import SimpleTestCase, tag
from django.test.signals import template_rendered

from hq.platform.core.interface_text import TEMPLATE_COMMENT
from hq.platform.core.stylesheets import STATIC, sheet_paths, stylesheet
from hq.platform.core.table_cells import ROLE_CLASSES
from hq_sdk.template_contract import CLASSES
lazy from hq.platform.core.tests.browser.catalog import CATALOG

from .source import template_files
from .test_style_usage import TEMPLATE_PREFIX

# The host's own templates, which no extension's check reads.
TEMPLATES = STATIC.parent / "templates"


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

    def test_every_face_is_preloaded_beside_the_stylesheet(self):
        """A face is shown only if it is ready for the first paint (lint:css
        holds each @font-face to `font-display: optional`), so its file is
        preloaded beside the stylesheet, where it usually is ready."""

        root = Path(__file__).resolve().parents[4]
        head = (root / "templates" / "base.html").read_text(encoding="utf-8")
        faces = [
            (sheet, face)
            for sheet in sheet_paths()
            for face in re.findall(r"@font-face\s*\{([^}]*)\}", sheet.read_text(encoding="utf-8"))
        ]
        self.assertTrue(faces)
        for sheet, face in faces:
            source = re.search(r'url\("([^"]+)"\)', face)
            # The face's address as `{% static %}` names it: relative to the sheet that asks for it.
            asset = (sheet.parent / source.group(1)).resolve().relative_to(STATIC).as_posix() if source else ""
            with self.subTest(face=asset or face[:60]):
                self.assertIsNotNone(source)
                self.assertIn(f'{{% static \'{asset}\' %}}" as="font"', head)

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
        """`display: flex` or `block` on a table part drops it out of table layout.

        The row then stacks into a single narrow column, and a colspan header
        collapses to one column's width. Neither fails loudly and both read as
        a styling nudge, so this reads the classes the templates actually put
        on table parts and rejects the declaration there. Lay out a div inside
        the cell instead. On a phone a wide table scrolls sideways in its
        `.table-scroll`; it does not stack into cards.
        """

        cell_classes = _table_part_classes()
        offences = []
        # Comments first: one sitting above a rule is otherwise read as part of
        # that rule's selector.
        source = re.sub(r"/\*.*?\*/", " ", self._stylesheet(), flags=re.DOTALL)
        for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
            declared = re.search(r"(?<![\w-])display\s*:\s*([a-z-]+)", block)
            if not declared or declared.group(1) not in _BOXES:
                continue
            offences.extend(
                f"{part.strip()} sets display: {declared.group(1)}"
                for part in selector.split(",")
                if part.strip() and _styles_a_table_part(part.strip(), declared.group(1), cell_classes)
            )

        self.assertEqual(sorted(set(offences)), [])

    def test_a_rows_block_detail_opens_beneath_the_row(self):
        """`row-more` shows a part in its own cell once the row is open, which
        suits a word or a list item. A block (a list, a paragraph, a panel)
        shown there stacks the whole detail into one column beside empty ones,
        so it is `row-below`, the line beneath the row at the table's width."""

        offences = [
            f"{name}: <{tag}>"
            for name, template in template_files()
            for tag in re.findall(
                r'<(div|ul|ol|p|section|form|table|dl)\b[^>]*class="[^"]*\brow-more\b',
                template.read_text(encoding="utf-8"),
            )
        ]
        self.assertEqual(offences, [])

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

    def test_user_menu_rows_share_one_element_independent_primitive(self):
        """A link and a POST action in one menu must render as one kind of row.

        Styling ``a`` and ``button`` separately lets the generic button
        primitive turn only the POST actions back into boxed controls. Every
        interactive row names the menu primitive instead, so adding another
        action cannot reintroduce that split by choosing the wrong element.
        """

        root = Path(__file__).resolve().parents[4]
        template = (root / "templates" / "partials" / "_user_menu.html").read_text(encoding="utf-8")
        panel = template.split('<div class="user-menu-panel"', 1)[1]
        rows = re.findall(r"<(?:a|button)\b[^>]*>", panel, re.DOTALL)

        self.assertTrue(rows)
        self.assertEqual([row for row in rows if "menu-item" not in row], [])
        self.assertIn(".menu-item {", self._stylesheet())

    def test_every_rule_sits_inside_a_cascade_layer(self):
        """Unlayered rules beat every layer, at any specificity.

        The effect compounds: a component written outside the layers cannot be overridden from
        `components`, so the only available fix is to write the next rule
        outside the layers too: a responsive rule nothing could reach,
        answered by another rule nothing could reach.

        Checked by brace depth rather than by parsing CSS: at depth zero the
        only thing allowed to open a block is an at-rule.
        """

        outside = []
        depth = 0
        for number, line in enumerate(self._stylesheet().splitlines(), 1):
            stripped = line.strip()
            if depth == 0 and "{" in stripped and not stripped.startswith(("@", "/*", "*")):
                outside.append(f"{number}: {stripped[:60]}")
            depth += line.count("{") - line.count("}")

        self.assertEqual(outside, [], "Rules outside @layer beat every layer. Put them in one.")

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

    def test_images_live_where_images_belong(self):
        """A screenshot taken while debugging is not an asset of this project.

        This repository is public, so a capture of any internal page is
        published the moment it is pushed (carrying whatever happened to be
        on screen) and force-pushing afterwards does not unpublish it.

        Images are diagrams, documentation captures, or icons, and each of
        those has a home. Anything outside them is something that arrived by
        accident.

        The question is what is *committed*, not what is on the disk, so it is
        asked of git rather than of the filesystem. A working tree holds plenty
        of images that are nobody's business (the Playwright MCP writes
        captures to `.playwright-mcp/`, which is ignored and therefore already
        safe) and a walk of the disk would either report those or need a
        hand-maintained list of directories to skip. Tracked files are exactly
        the ones that can be pushed.
        """

        root = Path(__file__).resolve().parents[4]
        allowed = ("docs/diagrams/", "docs/images/", "static/img/")
        suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")
        strays = [
            name for name in tracked if name and Path(name).suffix.lower() in suffixes and not name.startswith(allowed)
        ]
        self.assertEqual(
            sorted(strays),
            [],
            f"images belong in one of {allowed}",
        )

    def test_chart_templates_do_not_hardcode_the_plot_rectangle(self):
        """The geometry is in ui.py, and a copy of it in a template is a bug.

        A template that spells out plot coordinates stays behind when the plot
        moves: the bars and the line move, its gridlines and marks do not. Any
        literal that equals a plot coordinate is that fault.
        """

        from ..ui import PLOT_HEIGHT, PLOT_LEFT, PLOT_RIGHT, PLOT_TOP, PLOT_WIDTH

        forbidden = {
            str(round(value))
            for value in (
                PLOT_LEFT,
                PLOT_TOP,
                PLOT_LEFT + PLOT_WIDTH,
                PLOT_TOP + PLOT_HEIGHT,
                PLOT_LEFT + PLOT_WIDTH + PLOT_RIGHT,
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

    def test_chart_drawings_declare_no_fixed_pixel_floor(self):
        """A floor wider than the column it lives in is only ever a scrollbar.

        `.two-col` lays out at `minmax(320px, 1fr)`, so a half-width chart card
        offers roughly 284-446px. A floor above that range cannot protect a
        narrow plot; it only guarantees that every chart on the page scrolls
        at once.
        Label collision is handled by `Chart.dense` and a container query,
        which measure the labels and the card rather than guessing at a width.
        """

        css = self._stylesheet()
        for rule in ("bar-chart", "line-chart"):
            block = re.search(r"^\." + rule + r"\s*\{(.*?)\}", css, re.MULTILINE | re.DOTALL)
            self.assertIsNotNone(block, f".{rule} must exist")
            self.assertNotIn(
                "min-width",
                block.group(1),
                f".{rule} must not declare a fixed floor; it cannot be satisfied by a half-width card",
            )

    def test_scrollable_boxes_state_both_axes(self):
        """One declared scrollbar is two offered ones unless both are stated.

        A box with `overflow-x: auto` and no `overflow-y` does not keep the
        other axis at `visible`: the spec computes it to `auto`. Chart
        drawings land on fractional heights, so each overflowed itself by one
        rounded pixel and drew a full-height vertical scrollbar for it. Any
        rule that scrolls one axis has to say what the other one does.
        """

        css = self._stylesheet()
        offenders = []
        for selector, body in re.findall(r"^(\.[a-z0-9-]+)\s*\{([^}]*)\}", css, re.MULTILINE):
            has_x = re.search(r"overflow-x\s*:\s*(auto|scroll)", body)
            if not has_x:
                continue
            if not re.search(r"overflow-y\s*:", body):
                offenders.append(selector)
        self.assertEqual(
            offenders,
            [],
            "a rule that scrolls one axis must declare the other",
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

    def test_series_fills_use_categorical_slots_not_status_colors(self):

        css = self._stylesheet()
        reserved = {"--danger", "--warn", "--ok", "--attn"}
        for rule in re.findall(r"\.chart-series-\d+\s*\{([^}]*)\}", css):
            used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", rule))
            self.assertFalse(
                used & reserved,
                f"status colors are reserved and must not encode a series: {used & reserved}",
            )


# Decided at render time, so a word joined to one is not a literal class.
_INTERPOLATED = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def literal_classes(text: str) -> set[str]:
    """Every class a template names in full, outside its comments: in a class
    attribute, or as the css= a tag draws as one."""

    text = TEMPLATE_COMMENT.sub("", text)
    options = [css for tag in _INTERPOLATED.findall(text) for css in re.findall(r'\bcss="([^"]*)"', tag)]
    attributes = re.findall(r'\bclass="([^"]*)"', _INTERPOLATED.sub("\0", text))
    return {word for value in (*options, *attributes) for word in value.split() if "\0" not in word}


def built_prefixes(text: str) -> set[str]:
    """Each class a template builds at render time, as its prefix and ``*``:
    ``is-{{ state }}`` is ``is-*``."""

    text = re.sub(r"\{%.*?%\}", " ", re.sub(r"\{\{.*?\}\}", "{{}}", TEMPLATE_COMMENT.sub("", text), flags=re.DOTALL))
    attributes = re.findall(r'\bclass="([^"]*)"', text, flags=re.DOTALL)
    return {f"{prefix}*" for value in attributes for prefix in TEMPLATE_PREFIX.findall(value)}


def _drawn_by_the_catalog() -> dict[str, set[str]]:
    """The templates the component catalog draws, each with what of it is
    drawn: its own name for the whole file, else the ``{% partialdef %}``
    names drawn on their own."""

    drawn: dict[str, set[str]] = {}

    def record(sender, template, **kwargs):
        if template.origin and template.origin.template_name:
            drawn.setdefault(template.origin.template_name, set()).add(template.name)

    template_rendered.connect(record)
    try:
        for name, entry in CATALOG.items():
            entry.drawn(name)
    finally:
        template_rendered.disconnect(record)
    return drawn


def _fragment(text: str, name: str) -> re.Match[str] | None:
    return re.search(rf"\{{%\s*partialdef\s+{re.escape(name)}\b.*?\{{%\s*endpartialdef\b.*?%\}}", text, re.DOTALL)


def hand_rolled_classes() -> set[tuple[str, str]]:
    """Each class a host template draws that no shared part declares, with
    the template's name.

    A shared class is one ``hq_sdk``'s CLASSES publishes, a table role the
    region reads, or one a part the component catalog draws writes or builds.
    A part is declared by its catalog entry, so a page's markup moved into a
    partial with none is still the page's own: its classes count here."""

    drawn = _drawn_by_the_catalog()
    shared = set(CLASSES) | set(ROLE_CLASSES)
    pages = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        name, text = path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")
        whole = name in drawn.get(name, ())
        fragments = [found.group() for part in drawn.get(name, ()) if (found := _fragment(text, part))]
        for markup in [text] if whole else fragments:
            shared |= literal_classes(markup) | built_prefixes(markup)
        if not whole:
            for fragment in fragments:
                text = text.replace(fragment, "")
            pages.append((name, text))
    return {
        (name, word)
        for name, text in pages
        for word in literal_classes(text) | built_prefixes(text)
        if word not in shared
    }


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
            for word in literal_classes(path.read_text(encoding="utf-8"))
            if word not in defined
        )
        self.assertEqual(offenders, [], "define the class where its part is styled, or delete it")

    def test_host_templates_draw_only_the_shared_parts_classes(self):
        """A host page is drawn from the shared parts, so a class of its own is
        a hand-rolled part the survey never sees and an extension cannot draw.
        The classes held below are the pages not yet moved onto parts; the
        list only shrinks."""

        found = hand_rolled_classes()
        held = {(name, word) for name, words in HAND_ROLLED.items() for word in words.split()}
        self.assertEqual(
            sorted(f"templates/{name}: .{word}" for name, word in found - held),
            [],
            "draw it with a shared part or one of its options (hq_sdk.template_contract.CLASSES, or a template "
            "the component catalog draws); a widget with no part becomes one, with its own entry in "
            "hq/platform/core/tests/browser/catalog.py",
        )
        self.assertEqual(
            sorted(f"templates/{name}: .{word}" for name, word in held - found),
            [],
            "no longer hand-rolled: delete it from HAND_ROLLED",
        )

    def test_literal_classes_reads_attributes_and_tag_options(self):

        self.assertEqual(
            literal_classes(
                '<p class="note {{ tone }} tone-{{ x }}{% if a == "b" %} on {% endif %}">{% card css="frame wide" %}'
                '{% comment %}<b class="gone">{% endcomment %}{# class="gone" #}'
            ),
            {"note", "on", "frame", "wide"},
        )


# Each host template's classes that no shared part declares yet, by template.
# An entry leaves as its page moves onto a part, and none is ever added.
HAND_ROLLED: dict[str, str] = {
    "403.html": "notice-body notice-code notice-page",
    "404.html": "notice-body notice-code notice-page notice-search",
    "500.html": "notice-body notice-code notice-page",
    "analytics/overview.html": "bar-label bar-list bar-row bar-track bar-value chip reading-note window-switch",
    "auth/login.html": "auth-card auth-logo auth-note auth-sso",
    "base.html": "content",
    "calendars/_entry_fields.html": "field-range",
    "calendars/calendar.html": (
        "calendar-aside calendar-layout calendar-month calendar-source calendar-source-failure calendar-source-note "
        "calendar-sources calendar-swatch"
    ),
    "contacts/contact_detail.html": "quick-actions",
    "contacts/contact_list.html": "msg-preview row-unread status-tab",
    "control_plane/_connection_ability.html": "connection-evidence-*",
    "control_plane/_connection_read.html": "connection-evidence-* connection-sight-* connection-sight-state",
    "control_plane/_container_band.html": "github-gap",
    "control_plane/_container_detail.html": (
        "band-note hardening-head hardening-notes hardening-patch upgrade-blockers upgrade-reasons upgrade-steps"
    ),
    "control_plane/_credential_fix.html": (
        "connection-credential-fix connection-credential-fix-headline connection-credential-steps"
    ),
    "control_plane/_machine_band.html": "band-note",
    "control_plane/_machine_status.html": "page-title-meta",
    "control_plane/_path_hops.html": "hop-body hop-icon hop-label hop-proof-key path-proof-note request-path route",
    "control_plane/_record_status.html": "page-title-meta",
    "control_plane/_service_rows.html": "exposure exposure-* pin-move pin-toggle pinned service-name",
    "control_plane/_tool_dns.html": "lookup-grid",
    "control_plane/_topology_node_body.html": (
        "topology-hop topology-node-actions topology-relation topology-relation-* topology-relation-arrow "
        "topology-relation-heading topology-relation-kind topology-relation-link topology-relation-list "
        "topology-relation-other topology-relation-set topology-relation-source topology-relation-value "
        "topology-relation-values topology-relation-verb"
    ),
    "control_plane/_whatif_form.html": "control-label",
    "control_plane/_whatif_result.html": "verdict",
    "control_plane/_zone_records.html": "zone-value",
    "control_plane/connection_list.html": (
        "connection-ability-all connection-ability-list connection-ability-more connection-authority "
        "connection-authority-* connection-depends connection-more-scope connection-next connection-problems "
        "connection-reach connection-read-list connection-row-meta connection-sight-list connection-sight-refusal "
        "connection-standing connection-summary connection-table is-attention"
    ),
    "control_plane/container_list.html": "container-advisories container-reach github-gap",
    "control_plane/machine_detail.html": "node-key",
    "control_plane/resource_detail.html": "operation-evidence operation-result",
    "control_plane/service_detail.html": "control-origin exposure exposure-*",
    "control_plane/tools.html": "tool-summary tool-tabs",
    "control_plane/topology.html": (
        "topology-detail topology-hint topology-kind-* topology-kind-filters topology-kind-mark "
        "topology-lane topology-lens-note topology-map topology-node topology-node-* topology-node-degree "
        "topology-node-link topology-node-list topology-node-select topology-node-title topology-relations "
        "topology-search topology-stage topology-toolbar topology-trace-bar topology-trace-controls topology-view "
        "topology-workspace"
    ),
    "control_plane/zone_detail.html": "zone-switch zone-switch-note",
    "control_plane/zone_mail.html": (
        "control-label mail-record-value mail-records mail-stage-head mail-stage-question mail-stage-warning "
        "mail-stages"
    ),
    "core/_connection_loading.html": "conn-pending",
    "core/_connection_panel.html": (
        "conn conn-* conn-agree conn-agree-no conn-agree-yes conn-aside conn-carriage conn-carriage-* conn-check "
        "conn-headline conn-hop-detail conn-hop-more conn-hop-page conn-layer conn-layer-* conn-layer-body "
        "conn-layer-boundary conn-layer-evidence conn-layer-label conn-layers conn-link conn-link-* conn-link-line "
        "conn-more conn-note conn-path conn-peer conn-peer-name conn-peer-role conn-peering conn-row-note "
        "conn-rules conn-sub conn-tick conn-verdict-*"
    ),
    "core/_dashboard_connection.html": "dash-reading glance-caption glance-lead glance-panel-head glance-symbol",
    "core/_dashboard_controller.html": "dash-reading glance-caption glance-lead glance-panel-head glance-symbol",
    "core/_dashboard_glance.html": (
        "dashboard-glance glance-alert glance-caption glance-expanded glance-expanded-foot glance-expanded-head "
        "glance-freshness glance-hours glance-lead glance-lead-empty glance-note glance-panel glance-panel-head "
        "glance-panels glance-refresh glance-symbol is-wet status-*"
    ),
    "core/_dashboard_highlights.html": "dashboard-highlights highlight-card",
    "core/_dashboard_links_fields.html": "ext-choice-label ext-choice-sub ext-choices",
    "core/_dashboard_readings.html": "dash-readings",
    "core/_hq_status.html": "page-title-meta",
    "core/_policy_setting.html": "policy-select policy-unavailable rule-*",
    "core/_policy_table.html": "effect-* policy-default policy-effect policy-table",
    "core/agent_policy.html": "agent-events mcp-endpoint step-list",
    "core/auditlog_detail.html": "compare-table",
    "core/hq.html": "cell-notes",
    "dashboard.html": (
        "calendar-month dash-needs dash-start dashboard-patterns ext-arrow ext-links is-compact step-list"
    ),
    "hq_api/reference.html": "api-reference api-reference-failed api-reference-loading api-reference-status",
    "list_page.html": "select-column",
    "page.html": "page-foot",
    "partials/_demo_about.html": "demo-about-foot ext-arrow ext-links",
    "partials/_discovery_catalog.html": "discovery-link discovery-list discovery-main discovery-meta",
    "partials/_linked_text.html": "linked-text",
    "partials/_operator_step.html": "operator-step",
    "partials/_posture.html": "posture-checks step-list",
    "partials/_queue_groups.html": "queue-table",
    "partials/approval_card.html": "approval-actions approval-card approval-reason",
    "projects/_github_panel.html": "github-gap github-list github-mark workflow-chip workflow-chips",
    "projects/_project_meta.html": "project-meta",
    "projects/_updates.html": "github-list github-mark",
    "projects/posture.html": (
        "is-unmet posture-access posture-meter posture-meter-break posture-repo-name posture-repos posture-score"
    ),
    "projects/project_detail.html": "project-tags",
    "projects/watching.html": "gh-avatar gh-identity gh-name gh-profile gh-verified",
    "receipts/receipt_detail.html": "document-picture",
    "search.html": (
        "command-hint search-badge search-group search-hit search-hit-main search-hit-title search-hits "
        "search-scopes search-snippet search-summary"
    ),
}
