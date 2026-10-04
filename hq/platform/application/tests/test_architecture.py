from __future__ import annotations

import ast
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings
import httpx
from starlette.middleware.gzip import GZipMiddleware
from hq.platform.core.network import TrustedNetworkASGI
from starlette.staticfiles import StaticFiles

from hq.platform.core.static import CachedStaticFiles


def view_modules(root: Path) -> list[Path]:
    """Every web view module: ``views.py`` and the ``*_views.py`` split from one."""

    found = {*root.rglob("views.py"), *root.rglob("*_views.py")}
    assert found, "No view modules were scanned"
    return sorted(path for path in found if not path.name.startswith("test") and "tests" not in path.parts)



def _is_address(text: str, found: re.Match[str]) -> bool:
    """Whether a dotted quad in text is an address, not a version or an RFC section."""

    if any(int(part) > 255 for part in found.group().split(".")):
        return False
    return text[max(0, found.start() - 8):found.start()] != "section-"

class DeliveryAdapterArchitectureTests(SimpleTestCase):
    def test_workflow_models_remain_a_dependency_leaf(self):
        root = Path(__file__).parent.resolve().parent
        contracts = ast.parse(
            (root / "workflow_contracts.py").read_text(encoding="utf-8")
        )
        relative_imports = [
            node.module
            for node in ast.walk(contracts)
            if isinstance(node, ast.ImportFrom) and node.level
        ]
        self.assertEqual(relative_imports, [])

        boundaries = {
            "ui.py": "workflows",
            "workflows.py": "action_links",
        }
        for filename, forbidden in boundaries.items():
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level
            }
            self.assertNotIn(forbidden, imported)

    def test_asgi_routes_static_assets_before_django(self):
        from hq.config.asgi import application

        static_route, django_route = application.routes[-2:]
        self.assertEqual(static_route.path, "/static")
        # Outermost, because this mount is above the Django stack and would
        # otherwise be the one thing an untrusted caller could still fetch.
        self.assertIsInstance(static_route.app, TrustedNetworkASGI)
        compressed = static_route.app.app
        self.assertIsInstance(compressed, GZipMiddleware)
        self.assertIsInstance(compressed.app, StaticFiles)
        self.assertIsInstance(compressed.app, CachedStaticFiles)
        self.assertEqual(django_route.path, "")
        self.assertIsInstance(django_route.app, GZipMiddleware)

    @override_settings(STATIC_LIVE=False)
    def test_versioned_static_assets_are_compressed_and_immutable(self):
        async def request(root):
            app = GZipMiddleware(CachedStaticFiles(directory=root), minimum_size=500)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.get(
                    "/bundle.0123456789ab.css",
                    headers={"Accept-Encoding": "gzip"},
                )

        with TemporaryDirectory() as directory:
            Path(directory, "bundle.0123456789ab.css").write_text("a" * 2000, encoding="utf-8")
            manifest = SimpleNamespace(hashed_files={"bundle.css": "bundle.0123456789ab.css"})
            with patch("hq.platform.core.static.staticfiles_storage", manifest):
                response = async_to_sync(request)(directory)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    def test_mcp_services_do_not_access_django_models(self):
        """Keep MCP as an adapter over application-owned behavior and projections."""
        source_path = Path(__file__).resolve().parents[4] / "hq/platform/mcp" / "services.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        model_imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "models" or node.module.endswith(".models"))
        ]
        manager_access = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "objects"
        ]

        self.assertEqual(model_imports, [])
        self.assertEqual(manager_access, [])

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
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
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

    def test_paginated_list_views_use_the_shared_table_engine(self):
        root = Path(__file__).resolve().parents[4]
        violations = []
        for source_path in view_modules(root):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                is_paginated = any(
                    isinstance(item, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "paginate_by"
                        for target in item.targets
                    )
                    for item in node.body
                )
                bases = {
                    base.id for base in node.bases if isinstance(base, ast.Name)
                }
                if is_paginated and "TableListMixin" not in bases:
                    violations.append(f"{source_path.relative_to(root)}:{node.name}")

        self.assertEqual(violations, [])



    def test_every_module_defining_a_view_is_one_these_checks_read(self):
        """A view split into its own module must not step outside the checks.

        The two checks above read a fixed set of files. Any module under a
        Django app that subclasses a ``...View`` is a view module, and it is
        held to the same rules only if it is in that set.
        """

        root = Path(__file__).resolve().parents[4]
        read = set(view_modules(root))
        escaped = []
        for source_path in sorted(root.rglob("*.py")):
            if source_path.name.startswith("test") or source_path in read:
                continue
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.ClassDef)
                and any(
                    (base.id if isinstance(base, ast.Name) else getattr(base, "attr", ""))
                    .endswith("View")
                    for base in node.bases
                )
                for node in tree.body
            ):
                escaped.append(str(source_path.relative_to(root)))
        self.assertEqual(escaped, [])


class StyleContractTests(SimpleTestCase):
    """The style bundle is a contract that extensions render against.

    These guard failures that are invisible in review and silent at runtime:
    CSS resolves an undefined custom property to an invalid value rather than
    erroring, so a typo'd token degrades to a browser default (a black SVG
    fill) instead of breaking loudly.
    """

    @staticmethod
    def _stylesheet() -> str:
        root = Path(__file__).resolve().parents[4]
        return (root / "static" / "css" / "app.css").read_text(encoding="utf-8")

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

    def test_a_long_word_breaks_rather_than_widening_the_page(self):

        body = re.search(r"@layer base \{.*?\nbody \{([^}]*)\}", self._stylesheet(), re.S)
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
        source = re.sub(r"/\*.*?\*/", " ", self._stylesheet(), flags=re.S)
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

    def test_connection_content_cannot_choose_the_table_geometry(self):
        """A provider endpoint may be arbitrarily long but never owns layout."""

        css = self._stylesheet()
        # The table sizes to its content; the endpoint caps itself instead.
        self.assertNotIn(".connection-table { table-layout: fixed; }", css)
        endpoint_rule = css.split(".connection-endpoint {", 1)[1].split("}", 1)[0]
        # The cap every identifier in a table shares, so a narrow table can
        # cut it sooner in one place, and never more than the cell.
        self.assertIn("max-width: min(100%, var(--identifier-max));", endpoint_rule)
        self.assertIn("--identifier-max: 32ch;", css)
        self.assertIn("text-overflow: ellipsis;", endpoint_rule)
        self.assertIn("white-space: nowrap;", endpoint_rule)

    # Viewport breakpoints left, each for something the viewport genuinely
    # decides: the header and nav, a modal that takes the whole screen, table
    # sizing, the head's overflow menu, the dashboard strip, a few dense
    # diagrams. It only goes down. A layout answers to its own width with a
    # fluid rule (auto-fit, flex-wrap, clamp) or a container query instead.
    VIEWPORT_BREAKPOINTS = 10

    def test_viewport_breakpoints_only_go_down(self):

        css = re.sub(r"/\*.*?\*/", " ", self._stylesheet(), flags=re.S)
        found = [q for q in re.findall(r"@media[^{]*\{", css) if "width" in q]
        self.assertLessEqual(
            len(found),
            self.VIEWPORT_BREAKPOINTS,
            "A new viewport breakpoint: make the layout fluid or ask its container "
            "(docs/DESIGN.md, 'Size to content, fit the window').",
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

        css = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), self._stylesheet(), flags=re.S)
        seen: dict[str, list[int]] = collections.defaultdict(list)
        stack: list[str] = []
        start = 0
        for match in re.finditer(r"[{}]", css):
            if match.group() == "{":
                selector = " ".join(css[start:match.start()].split())
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
        panel = template.split('<div class="user-menu-panel">', 1)[1].split(
            "</details>", 1
        )[0]
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

        self.assertEqual(
            outside, [], "Rules outside @layer beat every layer. Put them in one."
        )

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
        lines in `.sweep-grid` are a 1px gap over a coloured background) and
        not a space at all.
        """


        on_scale = {2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40}
        declaration = re.compile(
            r"\b(?:padding|margin|gap|row-gap|column-gap)(?:-[a-z-]+)?:\s*([^;{}]+)",
        )
        offenders = sorted(
            {
                f"{value}px"
                for match in declaration.finditer(self._stylesheet())
                # calc() and max() carry their own arithmetic; the scale is
                # still the input, and rewriting inside them buys nothing.
                if not re.search(r"\b(?:calc|max|min|clamp)\(", match.group(1))
                for value in re.findall(r"(?<![-\w.])([0-9]+)px", match.group(1))
                if int(value) in on_scale
            }
        )

        self.assertEqual(
            offenders, [], "This spacing is on the scale; write it as --space-*."
        )

    # Every CSS named colour and system colour, so `color: tomato` is caught as
    # surely as a hex.
    _NAMED_COLOURS = frozenset(
        """aliceblue antiquewhite aqua aquamarine azure beige bisque black
        blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse
        chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
        darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta
        darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
        darkslateblue darkslategray darkslategrey darkturquoise darkviolet
        deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite
        forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green
        greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender
        lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
        lightgoldenrodyellow lightgray lightgreen lightgrey lightpink
        lightsalmon lightseagreen lightskyblue lightslategray lightslategrey
        lightsteelblue lightyellow lime limegreen linen magenta maroon
        mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
        mediumslateblue mediumspringgreen mediumturquoise mediumvioletred
        midnightblue mintcream mistyrose moccasin navajowhite navy oldlace
        olive olivedrab orange orangered orchid palegoldenrod palegreen
        paleturquoise palevioletred papayawhip peachpuff peru pink plum
        powderblue purple rebeccapurple red rosybrown royalblue saddlebrown
        salmon sandybrown seagreen seashell sienna silver skyblue slateblue
        slategray slategrey snow springgreen steelblue tan teal thistle tomato
        turquoise violet wheat white whitesmoke yellow yellowgreen
        canvas canvastext linktext visitedtext activetext buttonface
        buttontext buttonborder field fieldtext highlight highlighttext
        selecteditem selecteditemtext mark marktext graytext accentcolor
        accentcolortext""".split()
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

        css = re.sub(r"/\*.*?\*/", lambda match: blank(match.group()), cls._stylesheet(), flags=re.S)
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

    def test_colour_is_named_only_in_the_tokens_layer(self):
        """Colour lives in tokens, so a theme is a set of token overrides.

        The dark palette overrides `--bg`, `--ink` and the rest and nothing
        else. A literal anywhere else is a colour the theme cannot reach: it
        stays a light-mode value on a dark page, and nothing but a person
        looking at that one screen would notice. Literals also let related
        colours drift apart: hex written per component makes `published`,
        `reachable` and `success` three different greens.

        Allowed outside the layer: `var()` references, `color-mix()` of two
        tokens, and the keywords that are not a colour choice (`transparent`,
        `currentColor`, `inherit`).
        """


        literal = re.compile(
            r"#[0-9a-fA-F]{3,8}\b"
            r"|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\("
            r"|(?<![-\w.#$@])([a-zA-Z]+)(?![-\w(])"
        )
        offenders = []
        for line, value in self._declarations_outside_tokens():
            for match in literal.finditer(value):
                word = match.group(1)
                if word is None or word.lower() in self._NAMED_COLOURS:
                    offenders.append(f"{line}: {value.strip()[:70]}")
                    break

        self.assertEqual(
            offenders, [], "Colour belongs in a token in `@layer tokens`, not the component."
        )

    def test_the_colour_guard_reads_declarations(self):
        """The guard above would pass vacuously if its parser found nothing."""

        values = [value.strip() for _line, value in self._declarations_outside_tokens()]
        self.assertIn("var(--surface)", values)
        self.assertNotIn("#1f4d57", " ".join(values))

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
        # Bound before the attempt: skipTest raises, so the loop below is
        # unreachable when git is absent, but that is a fact about skipTest,
        # not one visible here.
        tracked: list[str] = []
        try:
            tracked = subprocess.run(
                # Tracked *and* new-but-not-ignored, so a file is checked by
                # the commit that first adds it rather than the one after.
                ["git", "ls-files", "-z", "--cached", "--others",
                 "--exclude-standard"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split("\0")
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("no git checkout to ask")

        # Documentation and private ranges are examples, not places. Anything
        # outside them is somewhere a packet can actually go.
        #
        # 192.168.0.0/16 is the exception: a host from it is somebody's actual
        # machine rather than an example. Only the range's base address passes,
        # since that appears solely as half of a CIDR. Fixtures wanting a
        # private address use 10.0.0.0/8, which every classifier here treats
        # identically.
        reserved = re.compile(
            r"^(?:127\.|10\.|192\.168\.0\.0$|169\.254\.|0\.|255\.|"
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
            r"(?:homelab|lan|internal|local)\b"
        )
        # Names a checkout's own deployment goes by, one per line, kept in
        # .git/info where nothing is tracked or pushed. Absent, only the
        # generic patterns above apply.
        terms_file = Path(
            subprocess.run(
                ["git", "rev-parse", "--git-path", "info/deployment-terms"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        if not terms_file.is_absolute():
            terms_file = root / terms_file
        try:
            terms = tuple(
                line.strip().lower()
                for line in terms_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            )
        except OSError:
            terms = ()
        # Lockfiles and pinned action SHAs are hashes, not hosts. Vendored
        # bundles/specs carry upstream examples, pinned by their UPSTREAM.
        skip = (
            "package-lock.json", "uv.lock", ".github/",
            "controller/api/vendor/", "static/vendor/",
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
            except (OSError, UnicodeDecodeError):
                continue
            for found in address.finditer(text):
                if _is_address(text, found) and not reserved.match(found.group()):
                    findings.append(f"{name}: {found.group()}")
            if host_key.search(text):
                findings.append(f"{name}: ssh host key")
            for candidate in sorted(set(private_host.findall(text))):
                findings.append(f"{name}: {candidate}")
            lowered = text.lower()
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
        # This suite also runs inside the composed image, which is the source
        # tree without the checkout that produced it: no .git, and no git
        # binary either. There is nothing to guard there: what is in the image
        # is already decided, and this asks what would be pushed. So it is
        # skipped rather than failed, and still runs everywhere the answer can
        # change.
        # Bound before the attempt: skipTest raises, so the read below is
        # unreachable when git is absent, but that is a fact about skipTest,
        # not one visible in this function, and reading a name that only some
        # branches assign is worth not writing either way.
        tracked: list[str] = []
        try:
            tracked = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split("\0")
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout; nothing here can be pushed")
        strays = [
            name
            for name in tracked
            if name
            and Path(name).suffix.lower() in suffixes
            and not name.startswith(allowed)
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
            for attribute, literal in re.findall(
                r'\b(x1|x2|y1|y2|cx|cy|width|height)="([0-9.]+)"', text
            ):
                if literal.split(".")[0] in forbidden:
                    offenders.append(f"{template.name}: {attribute}=\"{literal}\"")
        self.assertEqual(
            offenders,
            [],
            "chart templates must read plot coordinates from the chart, "
            "not restate them",
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
            block = re.search(
                r"^\." + rule + r"\s*\{(.*?)\}", css, re.MULTILINE | re.DOTALL
            )
            self.assertIsNotNone(block, f".{rule} must exist")
            self.assertNotIn(
                "min-width",
                block.group(1),
                f".{rule} must not declare a fixed floor; it cannot be "
                "satisfied by a half-width card",
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
        for selector, body in re.findall(
            r"^(\.[a-z0-9-]+)\s*\{([^}]*)\}", css, re.MULTILINE
        ):
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

    def test_series_fills_use_categorical_slots_not_status_colours(self):

        css = self._stylesheet()
        reserved = {"--danger", "--warn", "--ok", "--attn"}
        for rule in re.findall(r"\.chart-series-\d+\s*\{([^}]*)\}", css):
            used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", rule))
            self.assertFalse(
                used & reserved,
                f"status colours are reserved and must not encode a series: {used & reserved}",
            )


class SharedPrimitiveStyleTests(SimpleTestCase):
    """Shared partials may only use classes the style bundle actually defines.

    A class the bundle lacks raises no error; it is simply unstyled. The partials are the host's published UI
    contract, so anything they name has to exist here, otherwise the first
    surface to adopt a primitive is the one that discovers it is unstyled.
    """

    def test_partial_classes_are_defined_in_the_stylesheet(self):

        root = Path(__file__).resolve().parents[4]
        css = (root / "static" / "css" / "app.css").read_text(encoding="utf-8")
        defined = set(re.findall(r"\.([a-z][a-z0-9-]*)", css))
        offenders = []
        for template in sorted((root / "templates" / "partials").rglob("*.html")):
            text = template.read_text(encoding="utf-8")
            for attribute in re.findall(r'class="([^"]*)"', text):
                # Interpolated values are decided at render time; the pieces
                # that make them up are checked where they are defined instead.
                if "{{" in attribute or "{%" in attribute:
                    continue
                for name in attribute.split():
                    if name not in defined:
                        offenders.append(f"{template.name}: .{name}")
        self.assertEqual(sorted(set(offenders)), [])


class TemplateCommentTests(SimpleTestCase):
    """Django's {# #} comment is single-line only.

    A multi-line one is not a comment: it renders verbatim into the page. The
    failure is invisible in review because it looks exactly like a comment.
    """

    def test_no_multi_line_hash_comments_in_templates(self):

        root = Path(__file__).resolve().parents[4] / "templates"
        offenders = []
        for template in sorted(root.rglob("*.html")):
            text = template.read_text(encoding="utf-8")
            for match in re.finditer(r"\{#(.*?)#\}", text, re.DOTALL):
                if "\n" in match.group(1):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{template.relative_to(root)}:{line}")
            for number, line in enumerate(text.splitlines(), 1):
                if "{#" in line and "#}" not in line:
                    offenders.append(f"{template.relative_to(root)}:{number}")

        self.assertEqual(
            sorted(set(offenders)),
            [],
            "use {% comment %}…{% endcomment %} for multi-line comments",
        )


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
            f"{name}: {leaf}"
            for name, leaf in self.title_blocks()
            if "SITE_NAME" in leaf or "Severino HQ" in leaf
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
            path.name
            for path in self.workflows()
            if "vars.COMPOSITION_EXTENSIONS" in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(offenders, [], "read it from secrets, which are masked")


class AssertionPrecisionTests(SimpleTestCase):
    """A comparison belongs in the assertion, not inside a boolean.

    ``assertTrue(a > b)`` fails with "False is not true", which says nothing
    about a or b. ``assertGreater(a, b)`` prints both. CodeQL flags this; the
    check here finds it before a push does.
    """

    SPECIFIC = {
        "Eq": "assertEqual", "NotEq": "assertNotEqual",
        "Lt": "assertLess", "LtE": "assertLessEqual",
        "Gt": "assertGreater", "GtE": "assertGreaterEqual",
        "Is": "assertIs", "IsNot": "assertIsNot",
        "In": "assertIn", "NotIn": "assertNotIn",
    }

    def test_no_assertion_hides_a_comparison_inside_a_boolean(self):
        root = Path(__file__).resolve().parents[4]
        offenders = []
        for path in sorted(root.rglob("test*.py")) + sorted(root.rglob("tests.py")):
            # Any virtualenv in the tree, whatever it happens to be called, and
            # anything installed into one. Matching only ".venv" would miss a
            # sibling such as `.venv312` and report third-party test files as
            # offenders: a failure about the machine rather than the change.
            # Nor any hidden directory: a worktree under .claude/ is another
            # copy of this tree, checked in its own checkout.
            if any(
                part == "venv" or part.startswith(".") or part == "site-packages"
                for part in path.relative_to(root).parts
            ):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
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
                offenders.append(
                    f"{path.relative_to(root)}:{node.lineno}: use {better}"
                )

        self.assertEqual(offenders, [])


class SourceEscapeTests(SimpleTestCase):
    """An unknown escape in a string literal is a warning today, an error later.

    Python compiles ``"rgba\\("`` with a SyntaxWarning that scrolls past in a
    test run and becomes a SyntaxError in a later interpreter. JavaScript probes
    embedded in Python strings are where it happens: a regex needs its
    backslashes, so those strings are raw.
    """

    def test_no_string_literal_carries_an_invalid_escape(self):
        import warnings

        root = Path(__file__).resolve().parents[4]
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if any(
                part == "venv" or part.startswith(".") or part == "site-packages"
                for part in path.relative_to(root).parts
            ):
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                try:
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                except SyntaxError as error:
                    offenders.append(f"{path.relative_to(root)}:{error.lineno}: {error.msg}")

        self.assertEqual(offenders, [])


class CommentHistoryTests(SimpleTestCase):
    """A comment says what the code does and why, in the present tense.

    How the code came to be belongs in the commit message. Narration in a
    comment is read as a description of the code by everyone who opens the
    file, long after it stopped being true. The phrases below cannot describe
    present behaviour, so any of them in a comment is history.
    """

    NARRATION = re.compile(
        r"\b(used to be|the first version|the old version|reached production"
        r"|turned out|until now|until recently|this regression|the bug (?:that|was)"
        r"|shipped (?:once|unstyled|for a while)|we (?:had|used|found|added|removed|changed|tried))\b",
        re.IGNORECASE,
    )

    def _comments(self, path: Path, text: str):
        if path.suffix in (".css", ".js"):
            return re.finditer(r"/\*.*?\*/|//[^\n]*", text, re.S)
        if path.suffix in (".py", ".sh"):
            return re.finditer(r"#[^\n]*|\"\"\".*?\"\"\"", text, re.S)
        return re.finditer(r"\{% comment %\}.*?\{% endcomment %\}|\{#.*?#\}", text, re.S)

    SUFFIXES = {".py", ".js", ".css", ".html", ".sh"}
    # Generated or installed trees, and anything hidden (.git, .venv). The suite
    # also runs inside the image, where there is no git to ask what is tracked.
    SKIPPED = {"node_modules", "staticfiles", "build", "data", "var", "media", "exports", "backups"}

    def _sources(self, root: Path):
        for path in sorted(root.rglob("*")):
            parts = path.relative_to(root).parts
            if path.suffix not in self.SUFFIXES or not path.is_file():
                continue
            if any(part.startswith(".") or part in self.SKIPPED for part in parts[:-1]):
                continue
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

        rendered = Template('{{ n|counted:"thing needs you,things need you" }}').render(
            Context({"n": 1})
        )
        self.assertEqual(rendered, "1 thing needs you")


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
            for relative, text in self.sources("hq/platform/application", "hq/domains/control_plane", "hq/platform/core")
            if relative != "hq/domains/control_plane/names.py" and inline.search(text)
        ]
        self.assertEqual(found, [])

    def test_a_command_is_linked_through_command_url(self):
        """One builder, so a target is always encoded the way the form reads
        it: a hand-built ``?target=`` opened a form with nothing chosen."""


        inline = re.compile(r"""reverse\(\s*["']command["']""")
        scanned = list(self.sources("hq/platform/application", "hq/domains/control_plane", "hq/platform/core", "hq/platform/api", "hq/platform/mcp"))
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

    def test_zone_membership_is_asked_of_in_zone(self):

        inline = re.compile(r'endswith\(f"\.\{')
        found = [
            relative
            for relative, text in self.sources("hq/platform/application", "hq/domains/control_plane", "hq/platform/core")
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

    def test_templates_name_entities_through_the_entity_tag(self):

        # A button to a page (Cancel, Edit) is an action, not a mention.
        mention = re.compile(
            r'<a href="\{% url \'(control_plane:(detail|machine|service)|zones:detail|'
            r'projects:detail)\''
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
        ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
        ast.Try, ast.TryStar, ast.ExceptHandler, ast.Match,
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
                while (
                    len(rest) == 1
                    and isinstance(rest[0], ast.If)
                    and rest[0].col_offset == node.col_offset
                ):
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
                if path.name.startswith("test") or {"tests", "migrations"} & set(
                    relative.parts
                ):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                module = ".".join(relative.with_suffix("").parts)
                found.update(self.functions(tree, module))
        return found

    def test_the_score_weighs_nesting(self):
        source = (
            "def f(a):\n"
            "    for x in a:\n"          # +1
            "        if x and a:\n"      # +2
            "            pass\n"
            "        elif x:\n"          # +3
            "            pass\n"
            "        else:\n"
            "            with x:\n"      # +3
            "                pass\n"
            "    return [y for y in a if y] or None\n"
        )
        function = ast.parse(source).body[0]
        self.assertEqual(self.score(function), 9)

    def test_no_function_exceeds_the_limit(self):
        # No allowance list: a function over the limit is split, never excused.
        over = {
            name: score
            for name, score in self.scores().items()
            if score > self.LIMIT
        }
        self.assertEqual(over, {}, "split these into named steps")


class RouteOwnerTests(SimpleTestCase):
    """Routes are resolved in one place, so the answer can be remembered."""

    def test_the_application_resolves_routes_through_its_one_owner(self):
        root = Path(__file__).resolve().parents[4]
        offenders = [
            str(path.relative_to(root))
            for package in ("application", "control_plane")
            for path in (root / package).rglob("*.py")
            if "tests" not in path.parts
            and path.name != "routes.py"
            and re.search(r"django\.urls import[^\n]*\breverse\b", path.read_text(encoding="utf-8"))
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
