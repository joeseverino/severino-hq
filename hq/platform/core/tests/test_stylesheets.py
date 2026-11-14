"""The stylesheet is its sheets, all of them, linked in one cascade order."""

import re

from django.contrib.auth import get_user_model
from django.templatetags.static import static
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from hq.platform.application.tests.markup import elements, select
from hq.platform.core.stylesheets import DIRECTORY, SHEETS, STATIC, sheet_paths, stylesheet

LAYER_ORDER = re.compile(r"@layer\s+([\w\s,.-]+);")
LAYER_BLOCK = re.compile(r"@layer\s+([\w.-]+)\s*\{")
CONTAINER_QUERY = re.compile(r"@container\s+([^{]+)\{")
QUERY_NAME = re.compile(r"([a-zA-Z_][\w-]*)\s*(?=\(|not\b|style\()")
# A query of a custom property's value, asked or negated: a style query is
# asked of the element's parent, never of a size container.
STYLE_QUERY = re.compile(r"(?:not\s+)?style\(")
CONTAINER_NAMES = re.compile(r"(?:^|[{;])\s*container(?:-name)?\s*:\s*([^;{}/]+)")
# A query of whether a sticky container is stuck or scrolled, which is asked
# of a scroll-state container, never of a size one.
SCROLL_STATE_QUERY = re.compile(r"([a-zA-Z_][\w-]*)\s+scroll-state\(")
SCROLL_STATE = re.compile(r"\bscroll-state\b")
# A rule with no rule nested in it: its selector and its declarations.
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
CELL = re.compile(r"(?<![\w.#-])(?:td|th)(?![\w-])")
NOWRAP = re.compile(r"white-space\s*:\s*nowrap")
WRAPS = re.compile(r"white-space\s*:\s*(?:normal|pre-wrap|pre-line|break-spaces)|text-wrap-mode\s*:\s*wrap")
# A selector whose subject is a control: a `.btn` or a `button`, last in it.
BUTTON = re.compile(r"(?:\.btn\b|(?<![\w.#-])button\b)[^\s>+~,]*\)?\s*(?:,|$)")
CLIPPED = re.compile(r"overflow(?:-x)?\s*:\s*(?:hidden|clip)")
# The one-pixel clip that keeps text for a screen reader alone.
CLIP_RECIPE = re.compile(r"clip-path\s*:\s*inset\(50%\)")
# The sheets the table primitive is drawn in (docs/Design.md).
TABLE_PRIMITIVES = frozenset({"base.css", "cuts.css", "figures.css", "tables.css", "surfaces.css"})
FONT_FACE = re.compile(r"@font-face\s*\{[^{}]*\}")
FETCHED = re.compile(r"\burl\(|@import\b")


def _uncommented(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


@tag("source", "tree")
def _scroll_state_containers(css: str) -> set[str]:
    """The names a rule declares with a scroll-state container type."""

    names = set()
    for _selector, body in RULE.findall(css):
        if not SCROLL_STATE.search(body):
            continue
        names |= {name for found in CONTAINER_NAMES.findall(body) for name in found.split()}
    return names


def container_query_faults(css: str) -> tuple[list[str], list[str]]:
    """The container queries in ``css`` that name no container, and those
    that name one no sheet declares: a size query a declared container, a
    scroll-state query one declared with `container-type: scroll-state`."""

    declared = {name for names in CONTAINER_NAMES.findall(css) for name in names.split()}
    scrolled = _scroll_state_containers(css)
    unnamed, undeclared = [], []
    for prelude in (query.strip() for query in CONTAINER_QUERY.findall(css)):
        if STYLE_QUERY.match(prelude):
            continue
        if SCROLL_STATE.search(prelude):
            named = SCROLL_STATE_QUERY.match(prelude)
            if named is None:
                unnamed.append(prelude)
            elif named.group(1) not in scrolled:
                undeclared.append(prelude)
            continue
        named = QUERY_NAME.match(prelude)
        if named is None or named.group(1) == "not":
            unnamed.append(prelude)
        elif named.group(1) not in declared:
            undeclared.append(prelude)
    return unnamed, undeclared


def _leaf_rules(css: str) -> list[tuple[tuple[str, ...], str, str]]:
    """Each rule of ``css`` that holds declarations and no rule of its own:
    the preludes around it (``@layer base``, ``@media print``, a rule it is
    nested in), its selector and its declarations."""

    rules = []
    # Each open block's prelude, and whether a block has opened inside it.
    stack: list[tuple[str, bool]] = []
    start = 0
    for match in re.finditer(r"[{}]", css):
        if match.group() == "{":
            if stack:
                stack[-1] = (stack[-1][0], True)
            # A prelude follows the declaration or statement (`@layer a, b;`) before it.
            stack.append((" ".join(css[start : match.start()].rsplit(";", 1)[-1].split()), False))
        elif stack:
            prelude, holds_blocks = stack.pop()
            if not holds_blocks and not prelude.startswith("@"):
                rules.append((tuple(p for p, _ in stack), prelude, css[start : match.start()]))
        start = match.end()
    return rules


# A vendored app's mount: what each of HQ's layers gives way at (tokens.css).
VENDORED_MOUNT = ":where([data-vendored])"
# What styles the root alone, or the view transition's tree drawn over it.
ROOT_ONLY = (":root", "::view-transition")
ID_SELECTOR = re.compile(r"#[A-Za-z_][\w-]*")
CLASS_SELECTOR = re.compile(r"\.([A-Za-z_][\w-]*)")
NEGATION = re.compile(r":not\((?:[^()]|\([^()]*\))*\)")
# A reader's own setting, which holds for a vendor's markup as for HQ's.
READERS_OWN = ("@media print", "@media (prefers-reduced-motion: reduce)")


def _selector_list(selector: str) -> set[str]:
    """Each complex selector of a list, its commas inside `:is()` or
    `:not()` kept, whitespace collapsed."""

    parts, depth, start = [], 0, 0
    for at, char in enumerate(selector):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and not depth:
            parts.append(selector[start:at])
            start = at + 1
    return {" ".join(part.split()) for part in [*parts, selector[start:]]}


class VendoredMountTests(TestCase):
    def test_no_rule_of_hqs_reaches_a_vendored_mount(self):
        """No rule of HQ's stylesheet reaches markup a vendored app draws
        (`data-vendored` on its mount: the API reference's viewer), so the
        vendor's own rules alone style it. Each layer that holds a rule for
        any element but the root reverts all of its own declarations there,
        with one rule, the same in each, that outranks every other rule of
        the layer: no other selector names two ids. A rule no revert undoes
        (unlayered, or an important declaration) names a class that neither
        the vendored bundle nor the page around the mount writes, but for
        the reader's own settings (print, reduced motion), which hold for the
        vendor's markup too."""

        self.client.force_login(get_user_model().objects.create_user("operator"))
        page = self.client.get(reverse("api_reference:reference"))
        (mount,) = select(page, "data-vendored")
        around = {name for ancestor in mount.ancestors for name in ancestor.classes}
        bundle = (STATIC / "vendor" / "scalar" / "standalone.js").read_text(encoding="utf-8")

        def written(name):
            return name in around or re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", bundle) is not None

        def layer_of(preludes):
            return next((p.removeprefix("@layer ") for p in reversed(preludes) if p.startswith("@layer ")), "")

        reverts: dict[str, set[str]] = {}
        styled: set[str] = set()
        outranking, reaching = [], []
        for preludes, selector, body in _leaf_rules(_uncommented(stylesheet())):
            parts = _selector_list(selector)
            layer = layer_of(preludes)
            if "all: revert-layer" in " ".join(body.split()) and all(p.startswith(VENDORED_MOUNT) for p in parts):
                reverts.setdefault(layer, set()).update(parts)
                continue
            outranking += [part for part in parts if len(ID_SELECTOR.findall(part)) > 1]
            reach = {part for part in parts if not part.startswith(ROOT_ONLY)}
            if layer and reach:
                styled.add(layer)
            if (not layer or "!important" in body) and not set(READERS_OWN) & set(preludes):
                # A class named outside a negation, here or in a rule this one
                # is nested in, is one the element or a box around it carries.
                nesting = " ".join(p for p in preludes if not p.startswith("@"))
                reaching += [
                    part
                    for part in reach
                    if all(map(written, CLASS_SELECTOR.findall(NEGATION.sub("", f"{nesting} {part}"))))
                ]

        self.assertTrue(styled)
        self.assertEqual(sorted(styled - set(reverts)), [], "a layer that styles elements gives way at the mount")
        self.assertEqual(len({frozenset(parts) for parts in reverts.values()}), 1, "every layer gives way alike")
        self.assertIn(f"{VENDORED_MOUNT} :not(#_):not(#_)", next(iter(reverts.values())))
        self.assertEqual(outranking, [], "a selector of two ids ties the revert at the mount")
        self.assertEqual(sorted(reaching), [], "a rule no revert undoes names a class the vendor never writes")


class SheetTests(SimpleTestCase):
    def test_every_sheet_in_the_directory_is_linked(self):
        """A sheet left out of the list is never linked, and every check of the
        stylesheet reads past it."""

        written = {path.relative_to(STATIC).as_posix() for path in (STATIC / DIRECTORY).glob("*.css")}

        self.assertEqual(written, set(SHEETS))
        self.assertEqual(len(SHEETS), len(set(SHEETS)))

    def test_the_layer_order_is_declared_once_before_any_layer_opens(self):
        """A layer is ordered where it is first named. One a sheet opens
        without its declaration would be ordered after `components`, and its
        rules would beat every component's."""

        sheets = [_uncommented(path.read_text(encoding="utf-8")) for path in sheet_paths()]
        declared = [order for sheet in sheets for order in LAYER_ORDER.findall(sheet)]
        self.assertEqual(len(declared), 1)
        first = sheets[0]
        self.assertLess(first.index(declared[0]), LAYER_BLOCK.search(first).start())
        layers = {name.strip() for name in declared[0].split(",")}
        opened = {name for sheet in sheets for name in LAYER_BLOCK.findall(sheet)}
        self.assertLessEqual(opened, layers)

    def test_every_size_query_names_a_container_the_stylesheets_declare(self):
        """An unnamed size query is asked of whichever size container happens
        to be nearest, and of none once that one stops being a container, so it
        silently stops matching. A named one is asked of its own container, and
        a name nothing declares is the same silence."""

        css = "\n".join(_uncommented(path.read_text(encoding="utf-8")) for path in sheet_paths())
        unnamed, undeclared = container_query_faults(css)

        self.assertEqual(unnamed, [], "a size query names the container it is asked of")
        self.assertEqual(undeclared, [], "every container a query names is declared")

    def test_a_scroll_state_query_is_held_to_a_scroll_state_container(self):
        declared = ".bar { container: bar / scroll-state; } .size { container-name: size; }"

        self.assertEqual(
            container_query_faults(declared + "@container bar scroll-state(stuck: bottom) { .a { color: red; } }"),
            ([], []),
        )
        self.assertEqual(
            container_query_faults(declared + "@container size scroll-state(stuck: bottom) { .a { color: red; } }"),
            ([], ["size scroll-state(stuck: bottom)"]),
        )
        self.assertEqual(
            container_query_faults(declared + "@container scroll-state(stuck: bottom) { .a { color: red; } }"),
            (["scroll-state(stuck: bottom)"], []),
        )

    def test_only_the_table_primitives_hold_a_cells_line(self):
        """How wide a cell's line holds out is bounded by its column's share of
        the region (`--hold`, tables.css), so the holds of a table never sum
        past its box. A page's own nowrap on cell content holds out unbounded
        and widens the table past it; a page's width there is refused by
        `lint:css`. A nowrap that clips is a cut, bounded by its box, and
        stays allowed."""

        found = []
        for path in sheet_paths():
            if path.name in TABLE_PRIMITIVES:
                continue
            for selector, body in RULE.findall(_uncommented(path.read_text(encoding="utf-8"))):
                if not CELL.search(selector):
                    continue
                unbounded = NOWRAP.search(body) and not CLIPPED.search(body)
                if unbounded:
                    found.append(f"{path.name}: {' '.join(selector.split())}")

        self.assertEqual(found, [], "a cell's line is held only by the table primitives")

    def test_a_controls_label_keeps_its_line_from_the_button_rule_alone(self):
        """A label wrapped onto two or three lines reads as several controls
        and leaves the buttons beside it uneven. The button rule holds every
        label to one line, so no page or column restates it, and no rule
        lets one wrap while a column can give way to its labels. A control
        whose label is cut (cuts.css) holds its line for the cut."""

        rules = [
            (path.name, " ".join(selector.split()), body)
            for path in sheet_paths()
            for selector, body in RULE.findall(_uncommented(path.read_text(encoding="utf-8")))
        ]
        held = [name for name, selector, body in rules if selector == ".btn, button" and NOWRAP.search(body)]
        restated = [
            f"{name}: {selector}"
            for name, selector, body in rules
            if selector != ".btn, button"
            and BUTTON.search(selector)
            and NOWRAP.search(body)
            and not CLIPPED.search(body)
        ]

        # A flat line beneath an opened row on a phone stands its buttons one
        # to a line across it, so a label longer than the phone has nothing
        # left to give way and wraps.
        spans_the_line = 'tables.css: .data-table[data-line="flat"] .row-columns > dd .btn'
        wrapped = [
            f"{name}: {selector}"
            for name, selector, body in rules
            if BUTTON.search(selector) and WRAPS.search(body) and f"{name}: {selector}" != spans_the_line
        ]

        self.assertEqual(held, ["page.css"], "the button rule keeps a control's label on one line")
        self.assertEqual(restated, [], "a control's line is held by the button rule alone")
        self.assertEqual(wrapped, [], "a control's label keeps its line; its column gives way")

    def test_text_for_a_screen_reader_alone_is_hidden_by_the_one_class(self):
        """What only a screen reader hears is `.visually-hidden`
        (cadence.css), never the same recipe restated under another selector,
        so a change to how it is clipped is made once."""

        found = [
            " ".join(selector.split())
            for selector, body in RULE.findall(_uncommented(stylesheet()))
            if CLIP_RECIPE.search(body) and selector.strip() != ".visually-hidden"
        ]
        self.assertEqual(found, [])

    def test_a_sheet_fetches_nothing_but_its_faces(self):
        """An image or sheet a rule fetches arrives after the page is drawn and
        moves what it lands beside, past the moment the browser gate measures
        (`_DRAWN`). A face is the one fetch `document.fonts` reports, so the
        gate waits for it."""

        found = []
        for path in sorted((STATIC / "css").rglob("*.css")):
            css = FONT_FACE.sub(" ", _uncommented(path.read_text(encoding="utf-8")))
            found += [f"{path.relative_to(STATIC)}: {match.group()}" for match in FETCHED.finditer(css)]

        self.assertEqual(found, [], "a sheet fetches only a face, in @font-face")


class LinkedSheetTests(TestCase):
    def test_a_page_links_every_sheet_in_cascade_order(self):
        response = self.client.get(reverse("login"))

        links = [link.attrs["href"] for link in elements(response, "link") if link.attrs.get("rel") == "stylesheet"]
        self.assertEqual(links, [static(sheet) for sheet in SHEETS])


# A state's color is its tone, declared once in the tone classes (cadence.css),
# so a rule that colors an element by a state it names draws a second color for
# the same state. A state is named by a state class (`is-*`, `has-*`), a state
# attribute (`[data-state]`, `[data-ask-state]`) or a class that ends in a
# state or verdict word (`conn-verdict-failing`, `sweep-item-stale`).
STATE_CLASS = re.compile(r"\.(?:is|has)-[\w-]+")
STATE_ATTRIBUTE = re.compile(r"\[data-[\w-]*state\b")
STATE_WORD = re.compile(
    r"\.[\w-]*-(?:verdict|good|attention|serious|neutral|failing|failed|failure|confirmed|unconfirmed"
    r"|stale|unreachable|met|unmet|proven|contradicted|relay|relayed|idle|succeeded|lost)(?![\w-])"
)
# The colors a state draws in: the status colors and the tone tokens behind
# them. A tone class's own variables (`--toned`, `--tone-ink`) are its tone.
STATE_COLOR = re.compile(
    r"var\(--(?:ok|warn|danger|attn|tone-(?:positive|caution|critical|info|notable|severe|special|neutral)-[\w-]+)(?![\w-])"
)
# The line, which a state or verdict word may draw a state not yet decided in.
LINE = re.compile(r"var\(--line(?![\w-])")
COLORED = re.compile(
    r"(?:^|;)\s*((?:color|background(?:-color)?|border(?:-[\w-]+)?|outline(?:-color)?|fill|stroke"
    r"|box-shadow|text-decoration(?:-color)?|accent-color|caret-color)\s*:[^;]*)"
)
# The tone classes themselves, the one place a tone takes its color.
TONE_CLASS = re.compile(r"\.toned-(?:good|attention|serious|neutral)")

# The sites that still color a state by hand, each a sheet and its selector.
# The list only shrinks: a site moved onto the tone classes leaves it.
HAND_COLORED_STATES = frozenset(
    {
        ("cadence.css", '.job-progress:is([data-state="failed"], [data-state="lost"]) .job-bar'),
        ("cadence.css", '.job-progress[data-state="succeeded"] .job-bar'),
        ("cards.css", ".reading-age.is-stale"),
        ("connection.css", ".check-ladder.is-route > .ladder-rung::before"),
        ("connection.css", '.check-ladder.is-route > .ladder-rung[data-state="does-not"]::before'),
        ("connection.css", '.check-ladder.is-route > .ladder-rung[data-state="unknown"]::before'),
        ("connection.css", ".conn-carriage-relay > strong"),
        ("connection.css", '.ladder-rung[data-state="does-not"] .ladder-mark'),
        ("connection.css", '.ladder-rung[data-state="unknown"] .ladder-mark'),
        ("connection.css", '.ladder-rung[data-state="unknown"]'),
        ("connection.css", ".sweep-item-stale .sweep-age"),
        ("connection.css", ".sweep-item-unreachable .sweep-age"),
        ("connection.css", ".sweep-item-unreachable"),
        ("cuts.css", '.page-head [data-reading-age][data-ask-state="failed"]::after'),
        ("forms.css", ".field.has-errors input, .field.has-errors textarea, .field.has-errors select"),
        ("layout.css", ".calendar-source-failure"),
        ("page.css", ".reading-standing.is-attention"),
        ("page.css", ".reading-standing.is-stale"),
        ("pages.css", '.ask[data-ask-state="failed"] .ask-note'),
        (
            "pages.css",
            '.visit-status:is(.ask[data-ask-state="done"] > *, :root:has(.ask[data-ask-state="done"] > #page-visit) [form="page-visit"])::before',
        ),
        (
            "pages.css",
            '.visit-status:is(.ask[data-ask-state="failed"] > *, :root:has(.ask[data-ask-state="failed"] > #page-visit) [form="page-visit"])::before',
        ),
        ("routes.css", ".request-path li.is-attention .hop-icon"),
        ("routes.css", ".request-path li.is-attention::before"),
        ("routes.css", ".request-path li.is-proven .hop-icon, .hop-proof-key"),
        ("surfaces.css", ".kpi-dot.is-filled"),
        ("watching.css", ".posture-access .is-unmet"),
        ("watching.css", ".posture-meter .is-met"),
        ("watching.css", ".posture-meter .is-unmet"),
        ("watching.css", ".posture-score.is-met"),
        ("watching.css", ".posture-score.is-unmet"),
    }
)


def hand_colored_states(css: str) -> set[str]:
    """The selector of each rule in ``css`` that colors an element by a state
    it names, outside the tone classes, as written when nested."""

    found = set()
    for preludes, selector, body in _leaf_rules(_uncommented(css)):
        if TONE_CLASS.fullmatch(selector.strip()):
            continue
        written = " ".join([*(p for p in preludes if not p.startswith("@")), selector])
        worded = STATE_WORD.search(written)
        if not (worded or STATE_CLASS.search(written) or STATE_ATTRIBUTE.search(written)):
            continue
        colors = (STATE_COLOR, LINE) if worded else (STATE_COLOR,)
        if any(color.search(declaration) for declaration in COLORED.findall(body) for color in colors):
            found.add(" ".join(written.split()))
    return found


@tag("source")
class StateColorTests(SimpleTestCase):
    def test_a_state_takes_its_color_from_its_tone(self):
        """A state colored by a rule of its own draws one state in two colors
        beside a status drawn in its tone: set the element's ``toned-*`` class
        from the tone the state carries and read ``var(--toned)``."""

        found = {
            (path.name, selector)
            for path in sheet_paths()
            for selector in hand_colored_states(path.read_text(encoding="utf-8"))
        }

        self.assertEqual(sorted(found - HAND_COLORED_STATES), [], "a rule colors a state outside the tone classes")
        self.assertEqual(sorted(HAND_COLORED_STATES - found), [], "a listed site is gone: delete it from the list")

    def test_the_reader_finds_each_shape_of_a_hand_colored_state(self):
        refused = [
            ".panel.conn-verdict-failing { border-left-color: var(--attn); }",
            ".a.is-good { border-left-color: var(--ok); }",
            '.a[data-state="unknown"] .b { background: color-mix(in srgb, var(--warn) 38%, var(--line)); }',
            ".a-unconfirmed .line { background: var(--line); }",
            ".a { .is-unmet { color: var(--danger); } }",
            ".a.has-errors input { border-color: var(--danger); }",
            '.a[data-state="succeeded"] .bar { color: var(--ok, var(--accent)); }',
            ".a.is-attention .icon { background: linear-gradient(var(--tone-critical-tint) 0 0), var(--surface); }",
        ]
        allowed = [
            ".toned-good { --toned: var(--ok); }",
            ".a.toned-serious { border-inline-start: 3px solid var(--toned); }",
            ".btn.danger { color: var(--danger); }",
            ".a.is-related { border-color: color-mix(in srgb, var(--accent) 45%, var(--surface)); }",
            ".a.is-good { font-weight: 600; }",
            ".a.is-pane thead th { border-bottom: 1px solid var(--line); }",
            ".fact:is(.tone-good, .tone-serious) .fact-value { color: var(--tone-ink); }",
        ]
        for css in refused:
            with self.subTest(css=css):
                self.assertTrue(hand_colored_states(css))
        for css in allowed:
            with self.subTest(css=css):
                self.assertFalse(hand_colored_states(css))
