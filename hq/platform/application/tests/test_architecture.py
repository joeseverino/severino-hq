import ast
import gzip
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings, tag
from starlette.staticfiles import StaticFiles

from hq.platform.core.network import TrustedNetworkASGI
from hq.platform.core.static import CachedStaticFiles
from hq.platform.core.stylesheets import STATIC, sheet_paths, stylesheet
from hq_sdk import validation
from hq_sdk.validation import _cell_markup_pins

from .source import parsed, source_files


def view_modules(root: Path) -> list[Path]:
    """Every web view module: ``views.py`` and the ``*_views.py`` split from one."""

    found = {*source_files(root, "views.py"), *source_files(root, "*_views.py")}
    assert found, "No view modules were scanned"
    return sorted(path for path in found if not path.name.startswith("test") and "tests" not in path.parts)


def _is_address(text: str, found: re.Match[str]) -> bool:
    """Whether a dotted quad in text is an address, not a version or an RFC section."""

    if any(int(part) > 255 for part in found.group().split(".")):
        return False
    return text[max(0, found.start() - 8) : found.start()] != "section-"


class DeliveryAdapterArchitectureTests(SimpleTestCase):
    @tag("source")
    def test_workflow_models_remain_a_dependency_leaf(self):
        root = Path(__file__).parent.resolve().parent
        contracts = parsed(root / "workflow_contracts.py")
        relative_imports = [
            node.module for node in ast.walk(contracts) if isinstance(node, ast.ImportFrom) and node.level
        ]
        self.assertEqual(relative_imports, [])

        boundaries = {
            "ui.py": "workflows",
            "workflows.py": "action_links",
        }
        for filename, forbidden in boundaries.items():
            tree = parsed(root / filename)
            imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level}
            self.assertNotIn(forbidden, imported)

    def test_asgi_routes_static_assets_before_django(self):
        from django.conf import settings

        from hq.config import asgi
        from hq.config.asgi import application

        static_route, django_route = application.routes[-2:]
        self.assertEqual(static_route.path, "/static")
        # Outermost, because this mount is above the Django stack and would
        # otherwise be the one thing an untrusted caller could still fetch.
        self.assertIsInstance(static_route.app, TrustedNetworkASGI)
        # Nothing between the gate and the files: an asset is compressed when
        # the image is built, never while it is served.
        self.assertIsInstance(static_route.app.app, StaticFiles)
        self.assertIsInstance(static_route.app.app, CachedStaticFiles)
        self.assertEqual(django_route.path, "")
        # Django's own stack, compressed by Django's own middleware.
        self.assertIs(django_route.app, asgi.django_application)
        self.assertIn("django.middleware.gzip.GZipMiddleware", settings.MIDDLEWARE)

    def test_static_assets_have_one_server(self):
        from django.conf import settings

        serving = [name for name in settings.MIDDLEWARE if "static" in name.lower() or "whitenoise" in name.lower()]
        self.assertEqual(serving, [], "/static/ is served by the ASGI mount alone")

    @override_settings(STATIC_LIVE=False)
    def test_versioned_static_assets_are_compressed_and_immutable(self):
        async def request(root):
            transport = httpx2.ASGITransport(app=CachedStaticFiles(directory=root))
            async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(
                    "/bundle.0123456789ab.css",
                    headers={"Accept-Encoding": "gzip"},
                )

        with TemporaryDirectory() as directory:
            Path(directory, "bundle.0123456789ab.css").write_text("a" * 2000, encoding="utf-8")
            compressed = gzip.compress(b"a" * 2000, mtime=0)
            Path(directory, "bundle.0123456789ab.css.gz").write_bytes(compressed)
            manifest = SimpleNamespace(hashed_files={"bundle.css": "bundle.0123456789ab.css"})
            with patch("hq.platform.core.static.staticfiles_storage", manifest):
                response = async_to_sync(request)(directory)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        # The copy collectstatic wrote, byte for byte: nothing compressed it again.
        self.assertEqual(response.headers["content-length"], str(len(compressed)))
        self.assertEqual(response.text, "a" * 2000)
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    @tag("source")
    def test_mcp_services_do_not_access_django_models(self):
        """Keep MCP as an adapter over application-owned behavior and projections."""
        source_path = Path(__file__).resolve().parents[4] / "hq/platform/mcp" / "services.py"
        tree = parsed(source_path)

        model_imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "models" or node.module.endswith(".models"))
        ]
        manager_access = [
            node.lineno for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "objects"
        ]

        self.assertEqual(model_imports, [])
        self.assertEqual(manager_access, [])

    @tag("source")
    def test_web_views_do_not_mutate_models_directly(self):
        """Web adapters may query for rendering, but writes belong to use cases."""
        root = Path(__file__).resolve().parents[4]
        violations = []
        instance_mutations = {"save", "delete"}
        manager_mutations = {
            "create",
            "get_or_create",
            "update_or_create",
            "bulk_create",
            "bulk_update",
        }

        for source_path in view_modules(root):
            tree = parsed(source_path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                is_instance_mutation = node.func.attr in instance_mutations
                is_manager_mutation = (
                    node.func.attr in manager_mutations
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "objects"
                )
                if is_instance_mutation or is_manager_mutation:
                    violations.append(f"{source_path.relative_to(root)}:{node.lineno}")

        self.assertEqual(violations, [])

    @tag("source")
    def test_paginated_list_views_use_the_shared_table_engine(self):
        root = Path(__file__).resolve().parents[4]
        violations = []
        for source_path in view_modules(root):
            tree = parsed(source_path)
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                is_paginated = any(
                    isinstance(item, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "paginate_by" for target in item.targets)
                    for item in node.body
                )
                bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
                if is_paginated and "TableListMixin" not in bases:
                    violations.append(f"{source_path.relative_to(root)}:{node.name}")

        self.assertEqual(violations, [])

    @tag("source")
    def test_every_module_defining_a_view_is_one_these_checks_read(self):
        """A view split into its own module must not step outside the checks.

        The two checks above read a fixed set of files. Any module under a
        Django app that subclasses a ``...View`` is a view module, and it is
        held to the same rules only if it is in that set.
        """

        root = Path(__file__).resolve().parents[4]
        read = set(view_modules(root))
        escaped = []
        for source_path in source_files(root, "*.py"):
            if source_path.name.startswith("test") or source_path in read:
                continue
            tree = parsed(source_path)
            if any(
                isinstance(node, ast.ClassDef)
                and any(
                    (base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")).endswith("View")
                    for base in node.bases
                )
                for node in tree.body
            ):
                escaped.append(str(source_path.relative_to(root)))
        self.assertEqual(escaped, [])


@tag("source")
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

    def test_hidden_text_is_anchored_inside_its_box(self):
        """Left at its static position, an absolutely placed label escapes any
        scroll box whose containing block lies outside it: a hidden word in the
        last column of a wide table scrolled a phone's whole page sideways."""

        rule = re.search(r"\.visually-hidden\s*\{([^}]*)\}", self._stylesheet())
        self.assertIsNotNone(rule)
        body = rule.group(1)
        for declaration in ("position: absolute", "inset-block-start: 0", "inset-inline-start: 0"):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, body)

    def test_a_face_never_swaps_in_after_text_is_drawn(self):
        """A face that arrives late and is swapped in moves every line it sets.
        Each @font-face is shown only if ready for the first paint (optional),
        and its file is preloaded beside the stylesheet so it usually is."""

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
                self.assertIn("font-display: optional", face)
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

    def test_a_long_word_breaks_rather_than_widening_the_page(self):

        body = re.search(r"@layer base \{.*?\nbody \{([^}]*)\}", self._stylesheet(), re.DOTALL)
        self.assertIsNotNone(body)
        self.assertIn("overflow-wrap: break-word", body.group(1))

    @staticmethod
    def _script() -> str:
        root = Path(__file__).resolve().parents[4]
        return (root / "static" / "js" / "app.js").read_text(encoding="utf-8")

    def test_mobile_section_disclosures_keep_their_parent_drawer_open(self):
        """Opening a category must not dismiss the drawer containing it."""

        script = self._script()
        self.assertIn(
            'if (!menu.closest(".primary-nav")) setSectionMenu(false);',
            script,
        )
        css = self._stylesheet()
        self.assertIn(
            ".nav-is-open .primary-nav .nav-group > summary",
            css,
        )

    def test_a_table_part_is_never_given_a_block_flex_or_grid_display(self):
        """`display: flex` or `block` on a table part drops it out of table layout.

        The row then stacks into a single narrow column, and a colspan header
        collapses to one column's width. Neither fails loudly and both read as
        a styling nudge, so this reads the classes the templates actually put
        on table parts and rejects the declaration there. Lay out a div inside
        the cell instead. On a phone a wide table scrolls sideways in its
        `.table-scroll`; it does not stack into cards.
        """

        root = Path(__file__).resolve().parents[4]
        parts = {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}
        cell_classes: set[str] = set()
        for template in (root / "templates").rglob("*.html"):
            for attrs in re.findall(
                r"<(?:table|thead|tbody|tfoot|tr|td|th)\b([^>]*)>",
                template.read_text(encoding="utf-8"),
            ):
                found = re.search(r'class="([^"{}]*)"', attrs)
                if found:
                    cell_classes.update(found.group(1).split())

        boxes = {"block", "flex", "grid", "inline-flex", "inline-grid"}
        offences = []
        # Comments first: one sitting above a rule is otherwise read as part of
        # that rule's selector.
        source = re.sub(r"/\*.*?\*/", " ", self._stylesheet(), flags=re.DOTALL)
        for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
            declared = re.search(r"(?<![\w-])display\s*:\s*([a-z-]+)", block)
            if not declared or declared.group(1) not in boxes:
                continue
            for part in selector.split(","):
                # The subject is the rightmost compound selector: what the rule
                # actually styles, rather than what it is scoped by.
                trimmed = part.strip()
                if not trimmed:
                    continue
                subject = trimmed.split()[-1].split(">")[-1].strip()
                # A part's pseudo-element is a box inside the part, not the part:
                # giving it a display leaves the cell in table layout.
                if "::" in subject:
                    continue
                # An element qualifier settles it either way: `span.x` cannot
                # match a table part however `.x` is used elsewhere, and `td.x`
                # always does. Only an unqualified class has to be judged by
                # where the templates put it.
                qualifier = re.match(r"^([A-Za-z][\w-]*)", subject)
                if qualifier and qualifier.group(1) not in parts:
                    continue
                names = set(re.findall(r"\.([A-Za-z0-9_-]+)", subject))
                # `block` is common on spans that share a class with a cell
                # (`.muted`), so it is judged only where a table part is named.
                if declared.group(1) == "block" and not qualifier:
                    continue
                if qualifier or names & cell_classes:
                    offences.append(f"{trimmed} sets display: {declared.group(1)}")

        self.assertEqual(sorted(set(offences)), [])

    def test_a_rows_block_detail_opens_beneath_the_row(self):
        """`row-more` shows a part in its own cell once the row is open, which
        suits a word or a list item. A block (a list, a paragraph, a panel)
        shown there stacks the whole detail into one column beside empty ones,
        so it is `row-below`, the line beneath the row at the table's width."""

        root = Path(__file__).resolve().parents[4]
        offences = [
            f"{template.relative_to(root)}: <{tag}>"
            for template in (root / "templates").rglob("*.html")
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

    def test_every_referenced_custom_property_is_defined(self):

        css = self._stylesheet()
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.MULTILINE))
        referenced = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", css))
        # A fallback (var(--x, #fff)) is still a typo worth catching, so the
        # comparison deliberately ignores whether one was supplied.
        self.assertEqual(sorted(referenced - defined), [])

    def test_user_menu_rows_share_one_element_independent_primitive(self):
        """A link and a POST action in one menu must render as one kind of row.

        Styling ``a`` and ``button`` separately lets the generic button
        primitive turn only the POST actions back into boxed controls. Every
        interactive row names the menu primitive instead, so adding another
        action cannot reintroduce that split by choosing the wrong element.
        """

        root = Path(__file__).resolve().parents[4]
        template = (root / "templates" / "base.html").read_text(encoding="utf-8")
        panel = template.split('<div class="user-menu-panel">', 1)[1].split("</details>", 1)[0]
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

    def test_font_size_comes_from_the_type_scale(self):
        """A font size is one of the type scale's steps, or it is drift.

        Sizes half a pixel apart are not a distinction anyone can see.

        `em` is exempt. It means "relative to whatever this
        sits in" (a unit suffix shrinking beside its number, a glyph tracking
        its label) which is a different statement from choosing a step, and
        one an absolute scale cannot make.
        """

        literals = re.findall(r"font-size:\s*([0-9.]+(?:px|rem))", self._stylesheet())

        self.assertEqual(
            sorted(set(literals)),
            [],
            "font-size belongs to the --text-* scale, not a literal.",
        )

    def test_spacing_on_the_scale_is_written_as_a_token(self):
        """A value that is on the scale must say so.

        The even rungs are `--space-*`; this stops one being written as a
        literal.

        Odd values stay literals and are deliberately not failed here.
        Rounding padding by a pixel is visible in a dense table in a way that
        moving type by half a pixel is not, so each is judged on its own
        rather than swept. `1px` is exempt: it is a hairline rule (the grid
        lines in `.sweep-grid` are a 1px gap over a colored background) and
        not a space at all.
        """

        on_scale = {2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40}
        declaration = re.compile(
            r"\b(?:padding|margin|gap|row-gap|column-gap)(?:-[a-z-]+)?:\s*([^;{}]+)",
        )
        offenders = sorted(
            {
                f"{value}px"
                # calc() and max() carry their own arithmetic; the scale is
                # still the input, and rewriting inside them buys nothing.
                for match in declaration.finditer(self._stylesheet())
                if not re.search(r"\b(?:calc|max|min|clamp)\(", match.group(1))
                for value in re.findall(r"(?<![-\w.])([0-9]+)px", match.group(1))
                if int(value) in on_scale
            }
        )

        self.assertEqual(offenders, [], "This spacing is on the scale; write it as --space-*.")

    # Every CSS named color and system color, so `color: tomato` is caught as
    # surely as a hex.
    _NAMED_COLORS = frozenset(
        [
            "aliceblue",
            "antiquewhite",
            "aqua",
            "aquamarine",
            "azure",
            "beige",
            "bisque",
            "black",
            "blanchedalmond",
            "blue",
            "blueviolet",
            "brown",
            "burlywood",
            "cadetblue",
            "chartreuse",
            "chocolate",
            "coral",
            "cornflowerblue",
            "cornsilk",
            "crimson",
            "cyan",
            "darkblue",
            "darkcyan",
            "darkgoldenrod",
            "darkgray",
            "darkgreen",
            "darkgrey",
            "darkkhaki",
            "darkmagenta",
            "darkolivegreen",
            "darkorange",
            "darkorchid",
            "darkred",
            "darksalmon",
            "darkseagreen",
            "darkslateblue",
            "darkslategray",
            "darkslategrey",
            "darkturquoise",
            "darkviolet",
            "deeppink",
            "deepskyblue",
            "dimgray",
            "dimgrey",
            "dodgerblue",
            "firebrick",
            "floralwhite",
            "forestgreen",
            "fuchsia",
            "gainsboro",
            "ghostwhite",
            "gold",
            "goldenrod",
            "gray",
            "green",
            "greenyellow",
            "gray",
            "honeydew",
            "hotpink",
            "indianred",
            "indigo",
            "ivory",
            "khaki",
            "lavender",
            "lavenderblush",
            "lawngreen",
            "lemonchiffon",
            "lightblue",
            "lightcoral",
            "lightcyan",
            "lightgoldenrodyellow",
            "lightgray",
            "lightgreen",
            "lightgrey",
            "lightpink",
            "lightsalmon",
            "lightseagreen",
            "lightskyblue",
            "lightslategray",
            "lightslategrey",
            "lightsteelblue",
            "lightyellow",
            "lime",
            "limegreen",
            "linen",
            "magenta",
            "maroon",
            "mediumaquamarine",
            "mediumblue",
            "mediumorchid",
            "mediumpurple",
            "mediumseagreen",
            "mediumslateblue",
            "mediumspringgreen",
            "mediumturquoise",
            "mediumvioletred",
            "midnightblue",
            "mintcream",
            "mistyrose",
            "moccasin",
            "navajowhite",
            "navy",
            "oldlace",
            "olive",
            "olivedrab",
            "orange",
            "orangered",
            "orchid",
            "palegoldenrod",
            "palegreen",
            "paleturquoise",
            "palevioletred",
            "papayawhip",
            "peachpuff",
            "peru",
            "pink",
            "plum",
            "powderblue",
            "purple",
            "rebeccapurple",
            "red",
            "rosybrown",
            "royalblue",
            "saddlebrown",
            "salmon",
            "sandybrown",
            "seagreen",
            "seashell",
            "sienna",
            "silver",
            "skyblue",
            "slateblue",
            "slategray",
            "slategrey",
            "snow",
            "springgreen",
            "steelblue",
            "tan",
            "teal",
            "thistle",
            "tomato",
            "turquoise",
            "violet",
            "wheat",
            "white",
            "whitesmoke",
            "yellow",
            "yellowgreen",
            "canvas",
            "canvastext",
            "linktext",
            "visitedtext",
            "activetext",
            "buttonface",
            "buttontext",
            "buttonborder",
            "field",
            "fieldtext",
            "highlight",
            "highlighttext",
            "selecteditem",
            "selecteditemtext",
            "mark",
            "marktext",
            "graytext",
            "accentcolor",
            "accentcolortext",
        ]
    )

    @classmethod
    def _declarations_outside_tokens(cls) -> list[tuple[int, str]]:
        """Every declaration value outside `@layer tokens`, with its line.

        Comments are blanked (keeping their newlines, so line numbers hold),
        the tokens layer is blanked by brace depth, and what is left is split
        on `{`, `}` and `;`: a segment closed by `{` is a selector or an
        at-rule prelude and is skipped, so `.pill-green` cannot read as green.
        """

        def blank(text: str) -> str:
            return re.sub(r"[^\n]", " ", text)

        css = re.sub(r"/\*.*?\*/", lambda match: blank(match.group()), cls._stylesheet(), flags=re.DOTALL)
        for opening in reversed([match.start() for match in re.finditer(r"@layer\s+tokens\s*\{", css)]):
            depth, end = 0, len(css)
            for index in range(css.index("{", opening), len(css)):
                depth += {"{": 1, "}": -1}.get(css[index], 0)
                if depth == 0:
                    end = index + 1
                    break
            css = css[:opening] + blank(css[opening:end]) + css[end:]

        values = []
        for segment in re.finditer(r"[^{};]+(?=[;}])", css):
            name, colon, value = segment.group().partition(":")
            if colon and name.strip():
                values.append((css.count("\n", 0, segment.end()) + 1, value))
        return values

    def test_color_is_named_only_in_the_tokens_layer(self):
        """Color lives in tokens, so a theme is a set of token overrides.

        The dark palette overrides `--bg`, `--ink` and the rest and nothing
        else. A literal anywhere else is a color the theme cannot reach: it
        stays a light-mode value on a dark page, and nothing but a person
        looking at that one screen would notice. Literals also let related
        colors drift apart: hex written per component makes `published`,
        `reachable` and `success` three different greens.

        Allowed outside the layer: `var()` references, `color-mix()` of two
        tokens, and the keywords that are not a color choice (`transparent`,
        `currentColor`, `inherit`).
        """

        literal = re.compile(
            r"#[0-9a-fA-F]{3,8}\b"
            r"|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\("
            r"|(?<![-\w.#$@])([a-zA-Z]+)(?![-\w(])"
        )
        declarations = self._declarations_outside_tokens()
        # A parser that read nothing would find nothing wrong.
        self.assertGreater(len(declarations), 100)
        offenders = []
        for line, value in declarations:
            for match in literal.finditer(value):
                word = match.group(1)
                if word is None or word.lower() in self._NAMED_COLORS:
                    offenders.append(f"{line}: {value.strip()[:70]}")
                    break

        self.assertEqual(offenders, [], "Color belongs in a token in `@layer tokens`, not the component.")

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

        import subprocess

        root = Path(__file__).resolve().parents[4]
        tracked = subprocess.run(
            # Tracked *and* new-but-not-ignored, so a file is checked by
            # the commit that first adds it rather than the one after.
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")

        # Documentation, private, multicast and reserved ranges are examples or
        # classes, not places. Anything outside them is somewhere a packet can
        # actually go.
        #
        # 192.168.0.0/16 is the exception: a host from it is somebody's actual
        # machine rather than an example. Only the range's base address passes,
        # since that appears solely as half of a CIDR. Fixtures wanting a
        # private address use 10.0.0.0/8, which every classifier here treats
        # identically.
        reserved = re.compile(
            r"^(?:127\.|10\.|192\.168\.0\.0$|169\.254\.|0\.|255\.|22[4-9]\.|2[3-5][0-9]\.|"
            r"172\.(?:1[6-9]|2[0-9]|3[01])\.|"
            r"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|"
            r"203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|192\.0\.0\.|"
            r"1\.1\.1\.1|8\.8\.8\.8)"
        )
        address = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
        # Long enough to be key material rather than a fixture. A real
        # ed25519 public key is 68 base64 characters; tests legitimately use
        # short stand-ins, and failing on those would teach people to weaken
        # the check rather than fix a leak.
        host_key = re.compile(r"ssh-(?:ed25519|rsa) AAAA[A-Za-z0-9+/]{32,}")
        # A hostname under the deployment's own private zone. Not secret
        # (nothing outside the network resolves it) but it names one
        # installation's topology, and a public repository holds the shape
        # rather than the deployment. `example` and `invalid` are reserved for
        # writing about hostnames, which is what a fixture is doing.
        private_host = re.compile(
            r"\b[a-z0-9-]+\.(?!example\b|invalid\b|test\b|localhost\b)"
            r"(?:homelab|lan|internal|local)\b(?!\.(?:example\.)?toml\b)"
        )
        # Names a checkout's own deployment goes by, one per line, kept in
        # .git/info where nothing is tracked or pushed. A line starting with
        # `!` is an address the deployment publishes on purpose (a public
        # demo), which a file may name although a term covers it. Absent, only
        # the generic patterns above apply.
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
        terms = tuple(line for line in lines if not line.startswith("!"))
        # Lockfiles and pinned action SHAs are hashes, not hosts. Vendored
        # bundles/specs carry upstream examples, pinned by their UPSTREAM.
        skip = (
            "package-lock.json",
            "uv.lock",
            ".github/",
            "controller/api/vendor/",
            "static/vendor/",
        )

        findings = []
        for name in tracked:
            if not name or name.startswith(skip) or name.endswith(skip):
                continue
            if any(part in name for part in skip):
                continue
            path = root / name
            try:
                text = path.read_text(encoding="utf-8")
            except OSError, UnicodeDecodeError:
                continue
            findings.extend(
                f"{name}: {found.group()}"
                for found in address.finditer(text)
                if _is_address(text, found) and not reserved.match(found.group())
            )
            if host_key.search(text):
                findings.append(f"{name}: ssh host key")
            findings.extend(f"{name}: {candidate}" for candidate in sorted(set(private_host.findall(text))))
            lowered = text.lower()
            for address_published in published:
                lowered = lowered.replace(address_published, "")
            for index, term in enumerate(terms):
                if term in lowered:
                    findings.append(f"{name}: deployment term #{index + 1}")

        self.assertEqual(findings, [], f"reachable endpoints in tracked files: {findings}")

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
        import subprocess

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


@tag("source")
class SharedPrimitiveStyleTests(SimpleTestCase):
    """Shared partials may only use classes the style bundle actually defines.

    A class the bundle lacks raises no error; it is simply unstyled. The partials are the host's published UI
    contract, so anything they name has to exist here, otherwise the first
    surface to adopt a primitive is the one that discovers it is unstyled.
    """

    def test_partial_classes_are_defined_in_the_stylesheet(self):

        root = Path(__file__).resolve().parents[4]
        defined = set(re.findall(r"\.([a-z][a-z0-9-]*)", stylesheet()))
        offenders = []
        for template in sorted((root / "templates" / "partials").rglob("*.html")):
            text = template.read_text(encoding="utf-8")
            for attribute in re.findall(r'class="([^"]*)"', text):
                # Interpolated values are decided at render time; the pieces
                # that make them up are checked where they are defined instead.
                if "{{" in attribute or "{%" in attribute:
                    continue
                offenders.extend(f"{template.name}: .{name}" for name in attribute.split() if name not in defined)
        self.assertEqual(sorted(set(offenders)), [])


@tag("source")
class TemplateCommentTests(SimpleTestCase):
    """Django's {# #} comment is single-line only.

    A multi-line one is not a comment: it renders verbatim into the page. The
    failure is invisible in review because it looks exactly like a comment.
    The rule is ``interface_text.open_comment_lines``; ``InterfaceTextTests``
    holds every template of HQ and of each installed extension to it.
    """

    def test_a_comment_closed_on_its_line_is_fine(self):
        from hq.platform.core.interface_text import open_comment_lines

        self.assertEqual(open_comment_lines("<p>{# one #} and {# two #}</p>\n"), [])

    def test_a_comment_left_open_is_found_on_the_line_that_opens_it(self):
        from hq.platform.core.interface_text import open_comment_lines

        source = "<p>{# closed #} fine</p>\n<p>{# closed #} {# left\n   open #}</p>\n"

        self.assertEqual(open_comment_lines(source), [2])

    def test_a_template_root_is_read_whole(self):
        from hq.platform.core.interface_text import unclosed_template_comments

        with TemporaryDirectory() as root:
            Path(root, "partials").mkdir()
            Path(root, "page.html").write_text("<p>{# fine #}</p>\n", encoding="utf-8")
            Path(root, "partials", "_row.html").write_text("<td>\n{# left\n   open #}</td>\n", encoding="utf-8")

            self.assertEqual(unclosed_template_comments(root), ["partials/_row.html:2"])

    def test_the_sdk_exports_the_same_walk(self):
        from hq.platform.core import interface_text
        from hq_sdk import testing

        self.assertIs(testing.unclosed_template_comments, interface_text.unclosed_template_comments)


@tag("source")
class PageTitleTests(SimpleTestCase):
    """A page names itself; the site name is appended once, by the layout.

    Left to each page, a title either omits the site name or hardcodes a string
    ``SEVERINO_SITE_NAME`` is allowed to change. A title is not visible from
    inside the page that has it, so neither shows up in review.
    """

    def title_blocks(self):

        root = Path(__file__).resolve().parents[4] / "templates"
        pattern = re.compile(r"\{%\s*block title\s*%\}(.*?)\{%\s*endblock", re.DOTALL)
        for template in sorted(root.rglob("*.html")):
            if template.name == "base.html":
                continue
            match = pattern.search(template.read_text(encoding="utf-8"))
            if match:
                yield template.relative_to(root), match.group(1).strip()

    def test_no_page_appends_the_site_name_itself(self):
        offenders = [
            f"{name}: {leaf}" for name, leaf in self.title_blocks() if "SITE_NAME" in leaf or "Severino HQ" in leaf
        ]
        self.assertEqual(
            offenders,
            [],
            "base.html appends the site name: a page block names only itself",
        )

    def test_every_page_names_itself(self):
        offenders = [str(name) for name, leaf in self.title_blocks() if not leaf]
        self.assertEqual(
            offenders,
            [],
            "an empty title block renders as the site name preceded by a separator",
        )

    def test_the_layout_is_what_appends_it(self):
        base = Path(__file__).resolve().parents[4] / "templates" / "base.html"
        self.assertIn(
            "{% block title %}{% endblock %} · {{ SITE_NAME }}",
            base.read_text(encoding="utf-8"),
        )


@tag("source")
class WorkflowSecrecyTests(SimpleTestCase):
    """A private value reaches a public log through the environment or not at all.

    Actions expands ``${{ … }}`` into the text of the step it then echoes, so a
    value interpolated into a script body is printed in full before the step
    runs: ahead of anything the job does later to conceal it. Passed through
    ``env:`` it is a shell variable the echo never sees, and a secret is masked
    on top of that.
    """

    def workflows(self):
        root = Path(__file__).resolve().parents[4] / ".github" / "workflows"
        return sorted(root.glob("*.yml"))

    def run_block_lines(self, text: str):
        """Every line of every ``run:`` script, with its line number.

        Read by indentation rather than parsed, so this needs no YAML library
        and cannot start disagreeing with one about what a block contains.

        A one-line ``run:`` counts. Checking only block scalars would miss a
        one-line ``docker login`` piping a token straight into a shell.
        """

        lines = text.splitlines()
        inside = None
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if inside is not None:
                indent = len(line) - len(line.lstrip())
                if stripped and indent <= inside:
                    inside = None
                else:
                    yield number, line
                    continue
            if not stripped.startswith("run:"):
                continue
            if stripped.endswith(("|", ">", "|-", ">-")):
                inside = len(line) - len(line.lstrip())
            else:
                yield number, line

    def test_no_script_body_interpolates_a_secret_or_a_variable(self):
        offenders = []
        for path in self.workflows():
            for number, line in self.run_block_lines(path.read_text(encoding="utf-8")):
                if "${{ secrets." in line or "${{ vars." in line:
                    offenders.append(f"{path.name}:{number}")

        self.assertEqual(
            sorted(offenders),
            [],
            "pass it through env, instead: a script body is echoed verbatim",
        )

    def test_the_composition_set_is_read_from_a_secret(self):
        """A variable is not masked, and this inventory is the private half."""

        # Asserted on names rather than on contents: a failure that printed the
        # file would put the whole workflow in the output of the check meant to
        # keep things out of it.
        offenders = [
            path.name for path in self.workflows() if "vars.COMPOSITION_EXTENSIONS" in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(offenders, [], "read it from secrets, which are masked")


@tag("source")
class AssertionPrecisionTests(SimpleTestCase):
    """A comparison belongs in the assertion, not inside a boolean.

    ``assertTrue(a > b)`` fails with "False is not true", which says nothing
    about a or b. ``assertGreater(a, b)`` prints both. CodeQL flags this; the
    check here finds it before a push does.
    """

    SPECIFIC = {
        "Eq": "assertEqual",
        "NotEq": "assertNotEqual",
        "Lt": "assertLess",
        "LtE": "assertLessEqual",
        "Gt": "assertGreater",
        "GtE": "assertGreaterEqual",
        "Is": "assertIs",
        "IsNot": "assertIsNot",
        "In": "assertIn",
        "NotIn": "assertNotIn",
    }

    def test_no_assertion_hides_a_comparison_inside_a_boolean(self):
        root = Path(__file__).resolve().parents[4]
        offenders = []
        for path in source_files(root, "test*.py"):
            tree = parsed(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = getattr(node.func, "attr", "")
                if name not in ("assertTrue", "assertFalse"):
                    continue
                # `any(x > y for …)` is a Call, not a Compare, and is fine:
                # the comparison is part of the predicate being asserted.
                if not isinstance(node.args[0], ast.Compare):
                    continue
                operator = type(node.args[0].ops[0]).__name__
                better = self.SPECIFIC.get(operator, "a specific assertion")
                if name == "assertFalse":
                    better = "the negated form of " + better
                offenders.append(f"{path.relative_to(root)}:{node.lineno}: use {better}")

        self.assertEqual(offenders, [])


@tag("source")
class CellMarkupTests(SimpleTestCase):
    """A test reads a table cell by its text: the host may wrap what a cell says.

    One rule for the host and its extensions: ``hq_sdk.validation`` refuses it
    in an extension's tree, and this holds the host's own tests to it.
    """

    def test_no_test_pins_a_cell_closing_tag(self):
        root = Path(__file__).resolve().parents[4]
        offenders = [
            f"{path.relative_to(root)}:{line}"
            for path in source_files(root, "*.py")
            for line in _cell_markup_pins(parsed(path))
        ]
        self.assertEqual(offenders, [], "read the cell's text, not the tag that closes it")

    def test_a_pin_is_found_however_the_text_is_written(self):
        pinned = {
            "a literal": 'self.assertContains(response, "<td>Served</td>")',
            "a header": 'self.assertNotContains(response, "<th>Name</th>\\n")',
            "a keyword": 'self.assertContains(response, text="<td>Served</td>")',
            "an f-string": 'self.assertIn(f"<td>{name}</td>", html)',
            "a concatenation": 'self.assertNotIn("<td>" + name + "</td>", html)',
        }
        free = {
            "a cell's text": 'self.assertIn("Served", cells)',
            "an opening tag": 'self.assertContains(response, "<td>")',
            "a computed end": 'self.assertIn(f"<td>{tail}", html)',
            "the haystack": 'self.assertIn("Served", "<td>Served</td>")',
            "another assertion": 'self.assertEqual(cell, "<td>Served</td>")',
        }
        for name, line in pinned.items():
            with self.subTest(pinned=name):
                self.assertEqual(_cell_markup_pins(ast.parse(line)), [1])
        for name, line in free.items():
            with self.subTest(free=name):
                self.assertEqual(_cell_markup_pins(ast.parse(line)), [])

    def test_the_plugin_check_refuses_a_pin_and_says_what_to_read(self):
        with TemporaryDirectory() as source:
            tests = Path(source) / "example_plugin" / "tests"
            tests.mkdir(parents=True)
            (tests / "test_pages.py").write_text('def test(self, r):\n    self.assertContains(r, "<td>A</td>")\n')
            with patch("sys.argv", ["validation", source]), patch("builtins.print") as printed:
                self.assertEqual(validation.main(), 1)
        said = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("example_plugin/tests/test_pages.py:2", said)
        self.assertIn("read the cell's text", said)


@tag("source")
class CommentHistoryTests(SimpleTestCase):
    """A comment says what the code does and why, in the present tense.

    How the code came to be belongs in the commit message. Narration in a
    comment is read as a description of the code by everyone who opens the
    file, long after it stopped being true. The phrases below cannot describe
    present behavior, so any of them in a comment is history.
    """

    NARRATION = re.compile(
        r"\b(used to be|the first version|the old version|reached production"
        r"|turned out|until now|until recently|this regression|the bug (?:that|was)"
        r"|shipped (?:once|unstyled|for a while)|we (?:had|used|found|added|removed|changed|tried)"
        r"|now also|evaluated on)\b",
        re.IGNORECASE,
    )

    def _comments(self, path: Path, text: str):
        if path.suffix == ".md":
            # Prose outside fenced code, which keeps its offsets so a hit names its line.
            masked = re.sub(r"```.*?```", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.DOTALL)
            return re.finditer(r"(?:[^\n]+\n?)+", masked)
        if path.suffix in (".css", ".js", ".go"):
            return re.finditer(r"/\*.*?\*/|//[^\n]*", text, re.DOTALL)
        if path.suffix in (".py", ".sh"):
            return re.finditer(r"#[^\n]*|\"\"\".*?\"\"\"", text, re.DOTALL)
        return re.finditer(r"\{% comment %\}.*?\{% endcomment %\}|\{#.*?#\}", text, re.DOTALL)

    SUFFIXES = {".py", ".js", ".css", ".html", ".sh", ".go", ".md"}

    @staticmethod
    def _in_scope(relative: Path) -> bool:
        """Go and Markdown are read where HQ's own prose lives, not in vendored or generated trees."""

        if relative.suffix == ".go":
            return (
                relative.parts[0] == "controller"
                and "vendor" not in relative.parts
                and not relative.name.endswith(".gen.go")
            )
        if relative.suffix == ".md":
            return relative.parts[0] == "docs" or relative.parts in (("AGENTS.md",), ("README.md",))
        return True

    def _sources(self, root: Path):
        for path in source_files(root, "*"):
            if path.suffix in self.SUFFIXES and path.is_file() and self._in_scope(path.relative_to(root)):
                yield path

    def test_no_comment_narrates_history(self):
        root = Path(__file__).resolve().parents[4]
        sources = list(self._sources(root))
        self.assertGreater(len(sources), 100)
        found = []
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in self._comments(path, text):
                if phrase := self.NARRATION.search(match.group()):
                    line = text.count("\n", 0, match.start()) + 1
                    found.append(f"{path.relative_to(root)}:{line}: {phrase.group()}")
        self.assertEqual(found, [])


class CountedTests(SimpleTestCase):
    """One way to say how many, so a noun and its verb agree."""

    def test_the_phrase_agrees_for_one_and_for_many(self):
        from hq.platform.application.ui import counted

        self.assertEqual(counted(1, "needs output", "need output"), "1 needs output")
        self.assertEqual(counted(3, "needs output", "need output"), "3 need output")
        self.assertEqual(counted(0, "change"), "0 changes")
        self.assertEqual(counted(1200, "change"), "1,200 changes")

    def test_a_phrase_without_its_plural_is_refused(self):
        from hq.platform.application.ui import counted

        with self.assertRaises(ValueError):
            counted(2, "needs output")

    def test_the_template_filter_is_the_same_rule(self):
        from django.template import Context, Template

        rendered = Template('{{ n|counted:"thing needs you,things need you" }}').render(Context({"n": 1}))
        self.assertEqual(rendered, "1 thing needs you")


class InterfaceTextTests(SimpleTestCase):
    """What HQ and its extensions show people, read from source in one pass.

    A gate reads it, here. A running HQ never does: a system check runs before
    every management command, at every container start.
    """

    @classmethod
    def setUpClass(cls):
        from hq.platform.core import interface_text

        super().setUpClass()
        cls.reading = interface_text.read()

    def _places(self, found):
        return [f"{path}:{line}" + (f": {why[0]}" if why else "") for path, line, *why in found]

    def test_the_pass_read_the_source(self):
        self.assertGreater(self.reading.files, 300, "The interface text pass read almost nothing")

    def test_no_em_dash_in_interface_text(self):
        """Rewrite the sentence. For a missing value use `or_empty` or MISSING."""
        self.assertEqual(self._places(self.reading.em_dashes), [])

    def test_no_plural_built_by_hand(self):
        """Use counted(n, one, many) or {{ n|counted:"one,many" }}."""
        self.assertEqual(self._places(self.reading.hand_plurals), [])

    def test_no_form_opened_inside_another(self):
        """Close the outer form first, or point the control at a form with form="id"."""
        self.assertEqual(self._places(self.reading.nested_forms), [])

    def test_no_template_comment_is_left_open(self):
        """A {# #} comment ends on its own line. Use {% comment %}…{% endcomment %} for more."""
        self.assertEqual(self._places(self.reading.open_comments), [])

    def test_every_counted_phrase_agrees(self):
        """Give both forms: counted(n, "zone band", "zone bands")."""
        self.assertEqual(self._places(self.reading.unagreeable_counts), [])

    def test_no_system_check_that_runs_by_default_reads_source(self):
        import inspect

        from django.core.checks.registry import registry

        reads_source = re.compile(r"\b(ast\.parse|rglob|os\.walk|read_text|interface_text)\b")
        checked = 0
        for check in registry.get_checks(include_deployment_checks=False):
            module = inspect.getmodule(check)
            if module is None or not module.__name__.startswith("hq"):
                continue
            checked += 1
            with self.subTest(check=f"{module.__name__}.{check.__name__}"):
                self.assertIsNone(reads_source.search(inspect.getsource(module)))
        self.assertGreater(checked, 0, "No HQ system check was examined")


# What the plain-words rule reads, relative to the checkout: every template
# and every module, each read whole.
PLAIN_WORDS_PATHS = ("templates", "hq")
# Text under these is written for a program or the person writing one, in
# HQ's own vocabulary, and is never a sentence on a page: as (prefix, why).
PLAIN_WORDS_NOT_FOR_THE_OWNER = (
    ("hq/platform/api/", "The machine API: its errors and descriptions are read by a client's author."),
    ("hq/platform/mcp/", "Tool descriptions and errors an agent reads."),
    ("hq/domains/control_plane/bridge_", "The controller bridge: refusals the controller logs."),
    ("hq/domains/control_plane/provider_adapters/__init__.py", "Admission errors raised while HQ starts."),
    ("hq/domains/control_plane/provider_spec.py", "Declaration errors raised while HQ starts."),
    ("hq/domains/control_plane/providers.py", "Declaration errors raised while HQ starts."),
    ("hq/domains/control_plane/observations/contract.py", "Declaration errors raised while HQ starts."),
    ("hq/domains/control_plane/models.py", "Model validation messages no page shows."),
    ("hq/platform/application/integrations.py", "Errors an extension's author reads when it loads."),
    ("hq/platform/application/plugin_admission.py", "Admission errors raised while HQ starts."),
    ("hq/platform/core/outbound.py", "Why a request may wait on the outside, read by whoever adds one."),
    ("hq/platform/application/controller.py", "Refusals answered to the controller over the bridge."),
    ("hq/config/", "Settings and start-up errors."),
)


def _for_the_owner(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return not (
        "/tests/" in path
        or "/management/commands/" in path
        or name.startswith("test")
        or name in {"testing.py", "bench.py"}
        or name.endswith("_testing.py")
        or any(path.startswith(prefix) for prefix, _why in PLAIN_WORDS_NOT_FOR_THE_OWNER)
    )


# A place the rule lets a word stand, as (path, word, why). Each is a string
# that is kept as data or read by a client's author, never a sentence the
# owner is told. One that no longer matches fails the test.
PLAIN_WORDS_EXEMPT = (
    (
        "hq/platform/application/capability_policy.py",
        "Capability",
        "The type name stored on every past audit row for an agent rule; a new name would split that history in two.",
    ),
    *(
        (path, word, "Linux's own word for a privilege a container can be given, as Docker's cap_add spells it.")
        for path, word in (
            ("hq/platform/application/container_hardening.py", "capability"),
            ("hq/platform/application/container_standard.py", "capability"),
            ("hq/platform/application/container_standard.py", "capabilities"),
        )
    ),
    (
        "hq/platform/application/dns_findings.py",
        "provider",
        "'Your internet provider' is the everyday name for the company that sells the connection.",
    ),
    (
        "hq/platform/application/render_status_findings.py",
        "render",
        "Part of a literal log token the owner is told to search the journal for.",
    ),
    (
        "templates/control_plane/zone_detail.html",
        "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS",
        "The domain page's notice, owned by the pass reworking that page; it goes with the rewording there.",
    ),
    (
        "hq/domains/control_plane/apps.py",
        "Managed resource",
        "The type name stored on every past audit row for a record; a new name would split that history.",
    ),
    (
        "hq/platform/application/capabilities.py",
        "capability",
        "An error for a command name that does not exist, read by whoever wrote the caller.",
    ),
    (
        "hq/platform/application/cadence.py",
        "kind",
        "A validation message that lists the request's own field names for whoever wrote the caller.",
    ),
    (
        "hq/platform/application/infrastructure.py",
        "kind",
        "A validation message that lists the request's own field names for whoever wrote the caller.",
    ),
    (
        "hq/platform/application/resources.py",
        "resource",
        "An API error for a list name that does not exist, read by whoever "
        "wrote the client, in the API's own vocabulary.",
    ),
)


class PlainWordsTests(SimpleTestCase):
    """HQ's names for its own parts stay out of what its owner reads.

    ``hq.platform.core.plain_words`` holds the words and what to say instead.
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        from django.conf import settings

        from hq.platform.core import plain_words

        super().setUpClass()
        cls.words = plain_words
        root = Path(settings.BASE_DIR).resolve()
        found, cls.files = plain_words.read((root / path for path in PLAIN_WORDS_PATHS), root=root)
        cls.found = [item for item in found if _for_the_owner(str(item.path))]

    def test_the_rule_read_the_pages(self):
        self.assertGreater(self.files, 100, "The plain-words rule read almost nothing")

    def test_no_page_shows_a_word_hq_keeps_to_itself(self):
        """Say what plain_words.NEVER_SHOWN gives for it, or reword the sentence."""

        exempt = {(path, word) for path, word, _why in PLAIN_WORDS_EXEMPT}
        shown = [str(found) for found in self.found if (str(found.path), found.word) not in exempt]

        self.assertEqual(shown, [])

    def test_every_exemption_still_exempts_something_and_says_why(self):
        found = {(str(item.path), item.word) for item in self.found}
        for path, word, why in PLAIN_WORDS_EXEMPT:
            with self.subTest(path=path, word=word):
                self.assertIn((path, word), found, "Nothing here needs this exemption any more.")
                self.assertGreater(len(why.split()), 6, "Say why the word stands here.")

    def test_it_reads_what_a_person_sees_and_nothing_else(self):
        template = (
            "{% comment %}The sweep reconciles a kind.{% endcomment %}\n"
            '<a class="sweep-link" href="{% url \'control_plane:reconcile\' %}" title="Reconcile it">Open</a>\n'
            '{% include "partials/_sweep_freshness.html" with message="No resources yet." %}\n'
            "<code>adguard.rewrite</code><p>Findings</p><p>Read 3 records.</p>"
        )
        shown = [text for _line, text in self.words.template_text(template)]

        self.assertEqual(shown, ["Open", "Reconcile it", "No resources yet.", "Findings", "Read 3 records."])
        self.assertEqual(
            [word for text in shown for word, _instead in self.words.words_in(text)],
            ["Reconcile", "resources"],
        )

    def test_python_is_read_for_sentences_and_not_for_names(self):
        source = (
            '"""A sweep of every kind."""\n'
            'KIND = "adguard.rewrite"\n'
            'route = "control_plane:reconcile"\n'
            'logger.info("The sweep reached every provider.")\n'
            'title = "Was not in the last sweep"\n'
        )

        self.assertEqual([text for _line, text in self.words.python_text(source)], ["Was not in the last sweep"])

    def test_a_retired_name_is_found_as_written(self):
        self.assertEqual(
            [word for word, _instead in self.words.words_in("Open Action items from the Command Center")],
            ["Action items", "Command Center"],
        )
        self.assertEqual(list(self.words.words_in("3 things need you. Refresh all.")), [])

    def test_a_setting_name_or_a_field_value_is_found_even_in_code(self):
        template = (
            "<p>Set <code>SEVERINO_EXAMPLE_SWITCH</code> to allow it.</p>"
            "<p>Set manages to 1 on its item.</p><code>example.rewrite</code>"
        )
        found = [
            word
            for _line, text in (*self.words.template_text(template), *self.words.template_identifiers(template))
            for word, _instead in self.words.asked_in(text)
        ]

        self.assertEqual(sorted(found), ["SEVERINO_EXAMPLE_SWITCH", "manages to 1"])
        self.assertEqual(
            [word for word, _instead in self.words.words_in("Public DNS changes are off. Set SEVERINO_EXAMPLE_DNS.")],
            ["SEVERINO_EXAMPLE_DNS"],
        )

    def test_a_path_that_is_gone_fails_rather_than_passing(self):
        from pathlib import Path

        with self.assertRaises(FileNotFoundError):
            self.words.read([Path("templates/no-such-page.html")], root=Path())


class PostButtonTests(SimpleTestCase):
    """A post_button submits the one shared form, so it needs none of its own."""

    def render(self, source, **context):
        from django.template import Context, Template

        return Template(source).render(Context(context))

    def test_it_targets_the_shared_form_with_its_own_action(self):
        html = self.render('{% post_button "Move up" "/things/7/" value="up" title="Move up" %}')
        self.assertIn('form="hq-post"', html)
        self.assertIn('formaction="/things/7/"', html)
        self.assertIn('name="action" value="up"', html)
        self.assertIn('aria-label="Move up"', html)
        self.assertNotIn("disabled", html)

    def test_it_can_be_disabled(self):
        html = self.render('{% post_button "Refresh" "/sync/" disabled=True %}')
        self.assertIn(" disabled>", html)

    def test_its_label_is_escaped(self):
        html = self.render("{% post_button label '/x/' %}", label="<b>")
        self.assertIn("&lt;b&gt;", html)


@tag("source")
class OnePrimitiveTests(SimpleTestCase):
    """Questions asked in many places are answered by one function."""

    ROOT = Path(__file__).resolve().parents[4]
    # Modules another pass routes through the primitives.
    PENDING: set[str] = set()

    def sources(self, *packages):
        for package in packages:
            for path in sorted((self.ROOT / package).rglob("*.py")):
                relative = path.relative_to(self.ROOT).as_posix()
                if path.name.startswith("test") or relative in self.PENDING:
                    continue
                yield relative, path.read_text(encoding="utf-8")

    def test_hostnames_are_spelled_by_normalized_hostname(self):

        inline = re.compile(r'lower\(\)\s*\.rstrip\("\."\)|rstrip\("\."\)\s*\.lower\(\)')
        found = [
            relative
            for relative, text in self.sources(
                "hq/platform/application", "hq/domains/control_plane", "hq/platform/core"
            )
            if relative != "hq/domains/control_plane/names.py" and inline.search(text)
        ]
        self.assertEqual(found, [])

    def test_a_command_is_linked_through_command_url(self):
        """One builder, so a target is always encoded the way the form reads
        it: a hand-built ``?target=`` opened a form with nothing chosen."""

        inline = re.compile(r"""reverse\(\s*["']command["']""")
        scanned = list(
            self.sources(
                "hq/platform/application",
                "hq/domains/control_plane",
                "hq/platform/core",
                "hq/platform/api",
                "hq/platform/mcp",
            )
        )
        found = [
            relative
            for relative, text in scanned
            if relative != "hq/platform/application/action_links.py" and inline.search(text)
        ]
        self.assertIn("hq/platform/application/action_links.py", {relative for relative, _ in scanned})
        self.assertEqual(found, [])
        templates = [
            path.relative_to(self.ROOT).as_posix()
            for path in sorted((self.ROOT / "templates").rglob("*.html"))
            if re.search(r"""\{%\s*url\s+["']command["']""", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(templates, [])

    # Identifiers still written in bare code. Lower it as they move to
    # `|breakable`; never raise it.
    BARE_CODE_CEILING = 35

    def test_an_identifier_is_written_as_a_breakable_token(self):
        """A value in code wraps at its seams (`|breakable`), never inside
        "compose.y|aml" because its box lets prose break anywhere."""

        # A code block keeps its own lines, so code inside <pre> is not a token.
        bare = re.compile(r"(<pre[^>]*>\s*)?(<code[^>]*>\{\{(?:(?!\}\}).)*\}\})")
        found = [
            f"{path.relative_to(self.ROOT).as_posix()}: {code}"
            for path in sorted((self.ROOT / "templates").rglob("*.html"))
            for block, code in bare.findall(path.read_text(encoding="utf-8"))
            if not block and "|breakable" not in code
        ]
        self.assertLessEqual(len(found), self.BARE_CODE_CEILING, "\n".join(found))

    def test_zone_membership_is_asked_of_in_zone(self):

        inline = re.compile(r'endswith\(f"\.\{')
        found = [
            relative
            for relative, text in self.sources(
                "hq/platform/application", "hq/domains/control_plane", "hq/platform/core"
            )
            if relative != "hq/domains/control_plane/names.py" and inline.search(text)
        ]
        self.assertEqual(found, [])

    def test_templates_say_ages_through_the_ago_filter(self):
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sorted((self.ROOT / "templates").rglob("*.html"))
            if "|timesince }} ago" in path.read_text(encoding="utf-8")
            or "|timesince %}" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(found, [])

    def test_templates_say_byte_counts_through_the_bytes_filter(self):
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sorted((self.ROOT / "templates").rglob("*.html"))
            if "filesizeformat" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(found, [])

    def test_templates_write_amounts_through_the_money_filter(self):
        """``{{ amount|money }}``, never a dollar sign in front of a number filter,
        which writes a negative amount as "$-5.00"."""

        built = re.compile(r"\$\{\{")
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sorted((self.ROOT / "templates").rglob("*.html"))
            if built.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [])

    def test_a_related_list_is_drawn_by_its_partial(self):
        """A record page lists what it is related to through
        `partials/_related.html` (groups from `application.related`), never
        under a "Related" heading over a list of its own shape."""

        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sorted((self.ROOT / "templates").rglob("*.html"))
            if re.search(r"<h[1-6][^>]*>\s*Related\b", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [])

    def test_python_writes_amounts_through_money(self):
        """``application.money.money`` is the one place a dollar sign meets a
        number: no other f-string puts one in front of a value."""

        def builds_an_amount(path: Path) -> bool:
            for node in ast.walk(parsed(path)):
                if not isinstance(node, ast.JoinedStr):
                    continue
                for before, after in zip(node.values, node.values[1:], strict=False):
                    if (
                        isinstance(before, ast.Constant)
                        and str(before.value).endswith("$")
                        and isinstance(after, ast.FormattedValue)
                    ):
                        return True
            return False

        sources = [
            path
            for package in ("hq", "hq_sdk")
            for path in sorted((self.ROOT / package).rglob("*.py"))
            if "tests" not in path.parts
        ]
        self.assertTrue(sources)
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sources
            if path.relative_to(self.ROOT).as_posix() != "hq/platform/application/money.py" and builds_an_amount(path)
        ]
        self.assertEqual(found, [])

    def test_python_says_byte_counts_through_human_bytes(self):
        called = re.compile(r"import[^\n]*filesizeformat|filesizeformat\(")
        sources = [
            path
            for package in ("hq", "hq_sdk")
            for path in sorted((self.ROOT / package).rglob("*.py"))
            if "tests" not in path.parts
        ]
        self.assertTrue(sources)
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sources
            if called.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [])

    def test_templates_name_entities_through_the_entity_tag(self):

        # A button to a page (Cancel, Edit) is an action, not a mention.
        mention = re.compile(
            r'<a href="\{% url \'(control_plane:(detail|machine|service)|zones:detail|'
            r"projects:detail)\'"
        )
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sorted((self.ROOT / "templates").rglob("*.html"))
            if mention.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [])

    def test_entity_pages_are_addressed_by_entity_link(self):

        page = re.compile(
            r'reverse\(\s*"(control_plane:machine|control_plane:service|zones:detail|'
            r'projects:detail|control_plane:detail)"'
        )
        found = [
            relative
            for relative, text in self.sources("hq/platform/application")
            if relative != "hq/platform/application/entity_links.py"
            and not relative.endswith("views.py")
            and page.search(text)
        ]
        self.assertEqual(found, [])


@tag("source")
class CognitiveComplexityTests(SimpleTestCase):
    """No function asks a reader to hold more than twenty units of nesting.

    The score is the codebase-memory graph's ``cognitive`` property: +1 plus the
    nesting depth for each if, elif, loop, with, try, except and match. An elif
    or else body sits one level deeper than its if. Boolean operators,
    ternaries, comprehensions and nested functions add nothing.
    """

    LIMIT = 20
    ROOT = Path(__file__).resolve().parents[4]
    NESTING = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.With,
        ast.AsyncWith,
        ast.Try,
        ast.TryStar,
        ast.ExceptHandler,
        ast.Match,
    )

    @classmethod
    def score(cls, function) -> int:
        def block(nodes, nesting):
            return sum(walk(node, nesting) for node in nodes)

        def walk(node, nesting):
            if isinstance(node, ast.If):
                total = 1 + nesting + walk(node.test, nesting)
                total += block(node.body, nesting + 1)
                rest = node.orelse
                # An elif shares its if's column; an if inside an else does not.
                while len(rest) == 1 and isinstance(rest[0], ast.If) and rest[0].col_offset == node.col_offset:
                    branch = rest[0]
                    total += 2 + nesting + walk(branch.test, nesting + 1)
                    total += block(branch.body, nesting + 2)
                    rest = branch.orelse
                return total + block(rest, nesting + 1)
            children = ast.iter_child_nodes(node)
            if isinstance(node, cls.NESTING):
                return 1 + nesting + block(children, nesting + 1)
            return block(children, nesting)

        return block(function.body, 0)

    def functions(self, tree, prefix):
        for node in ast.iter_child_nodes(tree):
            name = f"{prefix}.{getattr(node, 'name', '')}"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield name, self.score(node)
            elif isinstance(node, ast.ClassDef):
                yield from self.functions(node, name)

    def scores(self) -> dict[str, int]:
        found = {}
        for package in sorted(self.ROOT.glob("*/__init__.py")):
            for path in sorted(package.parent.rglob("*.py")):
                relative = path.relative_to(self.ROOT)
                if path.name.startswith("test") or {"tests", "migrations"} & set(relative.parts):
                    continue
                tree = parsed(path)
                module = ".".join(relative.with_suffix("").parts)
                found.update(self.functions(tree, module))
        return found

    def test_the_score_weighs_nesting(self):
        source = (
            "def f(a):\n"
            "    for x in a:\n"  # +1
            "        if x and a:\n"  # +2
            "            pass\n"
            "        elif x:\n"  # +3
            "            pass\n"
            "        else:\n"
            "            with x:\n"  # +3
            "                pass\n"
            "    return [y for y in a if y] or None\n"
        )
        function = ast.parse(source).body[0]
        self.assertEqual(self.score(function), 9)

    def test_no_function_exceeds_the_limit(self):
        # No allowance list: a function over the limit is split, never excused.
        over = {name: score for name, score in self.scores().items() if score > self.LIMIT}
        self.assertEqual(over, {}, "split these into named steps")


@tag("source")
class FileSizeTests(SimpleTestCase):
    """No source file is longer than ``LIMIT`` lines.

    A file grows freely below it; one past it is split by responsibility.
    Every language HQ writes is held to it. Tests, migrations, generated code
    and vendored copies are not counted.
    """

    LIMIT = 1000
    ROOT = Path(__file__).resolve().parents[4]
    # The fewest files each language's walk can find in a checkout or the image.
    FLOOR = {"*.py": 100, "*.go": 50, "*.js": 4, "*.css": 4}
    NOT_WRITTEN_HERE = ("_tests.py", "_test.go", ".gen.go", ".min.js")

    def _counted(self, path: Path) -> bool:
        return (
            not path.name.startswith("test")
            and not path.name.endswith(self.NOT_WRITTEN_HERE)
            and not {"tests", "migrations", "node_modules", "vendor"} & set(path.relative_to(self.ROOT).parts)
        )

    def test_no_source_file_exceeds_the_ceiling(self):
        sizes = {}
        for pattern, floor in self.FLOOR.items():
            found = [path for path in source_files(self.ROOT, pattern) if self._counted(path)]
            self.assertGreater(len(found), floor, f"the {pattern} walk found almost nothing; its root is wrong")
            sizes |= {
                str(path.relative_to(self.ROOT)): len(path.read_text(encoding="utf-8").splitlines()) for path in found
            }
        over = {path: lines for path, lines in sizes.items() if lines > self.LIMIT}
        self.assertEqual(over, {}, "split these by responsibility")


@tag("source")
class OneDefinitionTests(SimpleTestCase):
    """A module-level name is assigned once.

    Python keeps the later of two assignments without a word, so a second
    constant of the same name quietly replaces the first everywhere it is read,
    a probe string included. Ruff's F811 covers functions and imports only.
    """

    def test_no_module_level_name_is_assigned_twice(self):
        root = Path(__file__).resolve().parents[4]
        twice = {}
        for path in source_files(root, "*.py"):
            counts: dict[str, int] = {}
            for node in parsed(path).body:
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                    if isinstance(node, ast.AnnAssign)
                    else []
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        counts[target.id] = counts.get(target.id, 0) + 1
            twice |= {f"{path.relative_to(root)}: {name}": n for name, n in counts.items() if n > 1}
        self.assertEqual(twice, {}, "give each its own name")


class RouteOwnerTests(SimpleTestCase):
    """Routes are resolved in one place, so the answer can be remembered."""

    @tag("source")
    def test_the_application_resolves_routes_through_its_one_owner(self):
        root = Path(__file__).resolve().parents[4]
        modules = [
            path
            for package in ("hq/platform/application", "hq/domains/control_plane")
            for path in (root / package).rglob("*.py")
            if "tests" not in path.parts and path.name != "routes.py"
        ]
        self.assertGreater(len(modules), 50, "the walk found almost nothing; its packages are not where it looks")
        offenders = [
            str(path.relative_to(root))
            for path in modules
            if re.search(r"django\.urls import[^\n]*\breverse\b", path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(offenders, [], "import reverse from application.routes")

    def test_a_remembered_route_is_djangos_and_follows_the_url_configuration(self):
        from django.urls import reverse as django_reverse

        from hq.platform.application.routes import reverse

        for name, kwargs in (("dashboard", None), ("control_plane:detail", {"key": "example"})):
            self.assertEqual(reverse(name, kwargs=kwargs), django_reverse(name, kwargs=kwargs))
            self.assertEqual(reverse(name, kwargs=kwargs), django_reverse(name, kwargs=kwargs))
        with self.settings(FORCE_SCRIPT_NAME="/under"):
            from django.urls import set_script_prefix

            set_script_prefix("/under/")
            try:
                self.assertEqual(reverse("dashboard"), django_reverse("dashboard"))
            finally:
                set_script_prefix("/")
        self.assertEqual(reverse("dashboard"), django_reverse("dashboard"))


class RequestNeverWaitsTests(SimpleTestCase):
    """A request answers from what HQ holds. It never reaches a network,
    starts a process or sleeps: the interpreter refuses it (``core.outbound``),
    whichever library the call came through."""

    ROOT = Path(__file__).resolve().parents[4]
    # Where each declared exception is entered. An entry here is a place a
    # request may wait: adding one is a decision, with its reason in ``ALLOWED``.
    ENTERED = {
        "oidc": {"hq/platform/core/oidc.py"},
        "lookup": {"hq/platform/application/lookup.py"},
    }

    def sources(self):
        paths = [
            path
            for package in ("hq", "hq_sdk")
            for path in sorted((self.ROOT / package).rglob("*.py"))
            if "tests" not in path.parts and not path.name.startswith("test")
        ]
        self.assertTrue(paths, "no source was found to check")
        for path in paths:
            yield path.relative_to(self.ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_every_request_is_served_under_the_rule(self):
        import time
        from unittest import mock

        from django.conf import settings
        from django.http import HttpResponse
        from django.test import RequestFactory

        from hq.platform.core.middleware import RequestContextMiddleware
        from hq.platform.core.outbound import OutboundInRequest

        def view(request):
            time.sleep(0)
            return HttpResponse("never")

        # First in the stack but for the network check, so every other
        # middleware and every view runs inside it.
        self.assertEqual(settings.MIDDLEWARE.index("hq.platform.core.middleware.RequestContextMiddleware"), 1)
        with (
            mock.patch("hq.platform.core.middleware.note_activity"),
            mock.patch("hq.platform.core.middleware.note_arrival"),
            self.assertRaises(OutboundInRequest),
        ):
            RequestContextMiddleware(view)(RequestFactory().get("/example/"))

    @tag("source")
    def test_an_exception_is_declared_with_its_reason_and_entered_only_where_listed(self):
        from hq.platform.core.outbound import ALLOWED

        entered: dict[str, set[str]] = {}
        for relative, source in self.sources():
            if "allowed(" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "allowed"):
                    continue
                argument = node.args[0] if node.args else None
                name = argument.value if isinstance(argument, ast.Constant) else "<not a literal>"
                entered.setdefault(str(name), set()).add(relative)

        self.assertEqual(entered, self.ENTERED)
        self.assertEqual(set(ALLOWED), set(self.ENTERED))
        for name, reason in ALLOWED.items():
            self.assertGreater(len(reason), 60, name)

    @tag("source")
    def test_only_a_job_leaves_the_request_that_started_it(self):
        leaving = {relative for relative, source in self.sources() if "off_request" in source}

        self.assertEqual(leaving, {"hq/platform/core/outbound.py", "hq/domains/jobs/runner.py"})

    @tag("source")
    def test_asked_for_work_is_followed_by_one_script_behavior(self):
        script = (self.ROOT / "static" / "js" / "ask.js").read_text(encoding="utf-8")
        templates = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted((self.ROOT / "templates").rglob("*.html"))
        )

        # The controls that ask for work outside the request are all an ask.
        for partial in ("_ask.html", "_job_progress.html", "_visit_refresh.html"):
            self.assertIn("data-ask", (self.ROOT / "templates" / "partials" / partial).read_text(encoding="utf-8"))
        for retired in ("data-job=", "data-visit-refresh", "data-job-note", "data-visit-status"):
            self.assertNotIn(retired, script + templates)
        self.assertEqual(script.count("[data-ask][data-ask-status]"), 1)


@tag("source")
class FragmentPrimitiveTests(SimpleTestCase):
    """Reads are one component: `static/js/fragment.js` and the part it asks for.

    A script that parses a response itself, or keeps its own timer to ask
    again, is a second fetch-and-swap with its own focus, session and failure
    behavior. Both are refused here, so the next read is an attribute on an
    element rather than a function in a page's script.
    """

    ROOT = Path(__file__).resolve().parents[4]
    PRIMITIVE = "fragment.js"
    # What only the primitive does: turn a response into markup, and ask again
    # on a timer.
    REFUSED = {
        "reads a response body as text": r"\.text\(\)",
        "reads a response body as data": r"\.json\(\)",
        "parses markup": r"DOMParser|parseFromString|insertAdjacentHTML|\.innerHTML\b|\.outerHTML\s*=",
        "keeps its own interval": r"\bsetInterval\(",
        "sleeps in a loop": r"setTimeout\(\s*resolve\b",
        "opens its own stream": r"\bEventSource\b|\bXMLHttpRequest\(",
        "fetches outside the session boundary": r"(?<![\w.])fetch\(|window\.fetch\(",
    }
    # How many times a script may still do what is refused, and why. Asked-for
    # work answers with how it stands (state, note, seconds, status address),
    # which every copy of its control on the page draws in place; that answer
    # is data no one region is drawn from: the ask's post and its status poll.
    ALLOWED = {("ask.js", "reads a response body as data"): 2}
    # The attributes of the hand-rolled reads the primitive replaced.
    RETIRED = (
        "data-deferred",
        "data-live-form",
        "data-whatif",
        "data-connection-source",
        "data-connection-slot",
        "data-dashboard-glance",
        "data-calendar-source",
        "X-Command-Center",
        "data-action-count-url",
    )

    def scripts(self) -> dict[str, str]:
        found = {
            path.name: path.read_text(encoding="utf-8") for path in sorted((self.ROOT / "static" / "js").glob("*.js"))
        }
        self.assertIn(self.PRIMITIVE, found)
        self.assertGreater(len(found), 1)
        return found

    def test_only_the_primitive_parses_a_response_or_asks_again_on_a_timer(self):
        for name, source in self.scripts().items():
            if name == self.PRIMITIVE:
                continue
            for what, pattern in self.REFUSED.items():
                with self.subTest(script=name, rule=what):
                    self.assertEqual(
                        len(re.findall(pattern, source)),
                        self.ALLOWED.get((name, what), 0),
                        f"{name} {what}; a read is a `data-fragment` region or "
                        "`hqFragment.swap`, and a poll is `hqEvery`",
                    )

    def test_the_primitive_is_one_fetch_one_parser_and_one_timer(self):
        source = self.scripts()[self.PRIMITIVE]

        self.assertEqual(source.count("window.fetch("), 1)
        self.assertEqual(source.count("parseFromString("), 1)
        self.assertEqual(source.count("window.setTimeout("), 1)
        self.assertNotIn("setInterval(", source)
        # The parser is not handed out: nothing outside the closure can reach it.
        self.assertIn("return { swap, reveal, fail };", source)
        self.assertFalse(re.search(r"window\.\w+\s*=\s*parse\b", source), "the parser left the closure")

    def test_the_primitive_loads_before_the_scripts_that_use_it(self):
        base = (self.ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        deferred = re.findall(r"js/([\w-]+\.js)'[^>]*\bdefer\b", base)

        self.assertGreater(len(deferred), 1)
        self.assertEqual(deferred[0], self.PRIMITIVE)

    def test_every_part_a_template_names_is_one_a_template_defines(self):
        sources = [path.read_text(encoding="utf-8") for path in sorted((self.ROOT / "templates").rglob("*.html"))]
        named = {name for text in sources for name in re.findall(r'data-fragment-name="(\w+)"', text)}
        defined = {name for text in sources for name in re.findall(r"{% partialdef (\w+)", text)}

        self.assertTrue(named)
        self.assertEqual(named - defined, set())

    def test_the_reads_it_replaced_stay_gone(self):
        texts = dict(self.scripts())
        for path in sorted((self.ROOT / "templates").rglob("*.html")):
            texts[str(path.relative_to(self.ROOT))] = path.read_text(encoding="utf-8")
        for name, text in texts.items():
            for retired in self.RETIRED:
                with self.subTest(file=name, retired=retired):
                    self.assertNotIn(retired, text)


@tag("source")
class TestPlacementTests(SimpleTestCase):
    """Every test lives in its package's tests/ directory (CLAUDE.md), so
    coverage, discovery and the fast gate's selection see the same tree."""

    def test_no_test_module_lives_outside_a_tests_directory(self):
        root = Path(__file__).resolve().parents[4]
        modules = [
            path
            for package in ("hq", "hq_sdk")
            for path in (root / package).rglob("test*.py")
            if path.name.startswith("test_") or path.name == "tests.py"
        ]
        self.assertTrue(modules)

        misplaced = sorted(
            path.relative_to(root).as_posix()
            for path in modules
            if "tests" not in path.parts and path.name != "test_runner.py"
        )
        self.assertEqual(misplaced, [])


@tag("source")
class RuntimeDependencyTests(SimpleTestCase):
    """Every runtime dependency earns its place: host code imports it, or
    pyproject.toml says why it is installed without being imported."""

    def test_every_runtime_dependency_is_imported_or_explained(self):
        import ast
        import importlib.metadata
        import tomllib

        root = Path(__file__).resolve().parents[4]
        project = tomllib.loads((root / "pyproject.toml").read_text())
        explained = set(project["tool"]["severino-hq"]["unimported"])
        imported: set[str] = set()
        for package in ("hq", "hq_sdk"):
            for path in (root / package).rglob("*.py"):
                if "tests" in path.parts:
                    continue
                for node in ast.walk(parsed(path)):
                    if isinstance(node, ast.Import):
                        imported |= {alias.name.split(".")[0] for alias in node.names}
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        imported.add(node.module.split(".")[0])
        providers = importlib.metadata.packages_distributions()
        used = {distribution.lower() for name in imported for distribution in providers.get(name, ())}
        declared = {
            re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower()
            for requirement in project["project"]["dependencies"]
        }

        self.assertEqual(sorted(declared - used - explained), [])
        # A reason for something that is imported, or no longer installed, is stale.
        self.assertEqual(sorted(explained & used), [])
        self.assertEqual(sorted(explained - declared), [])
