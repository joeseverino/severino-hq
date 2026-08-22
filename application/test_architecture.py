from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
import httpx
from starlette.middleware.gzip import GZipMiddleware
from core.network import TrustedNetworkASGI
from starlette.staticfiles import StaticFiles

from core.static import CachedStaticFiles


class DeliveryAdapterArchitectureTests(SimpleTestCase):
    def test_asgi_routes_static_assets_before_django(self):
        from config.asgi import application

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

    def test_versioned_static_assets_are_compressed_and_immutable(self):
        async def request(root):
            app = GZipMiddleware(CachedStaticFiles(directory=root), minimum_size=500)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.get(
                    "/bundle.css?v=content-hash",
                    headers={"Accept-Encoding": "gzip"},
                )

        with TemporaryDirectory() as directory:
            Path(directory, "bundle.css").write_text("a" * 2000, encoding="utf-8")
            response = async_to_sync(request)(directory)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    def test_mcp_services_do_not_access_django_models(self):
        """Keep MCP as an adapter over application-owned behavior and projections."""
        source_path = Path(__file__).resolve().parents[1] / "hq_mcp" / "services.py"
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
        root = Path(__file__).resolve().parents[1]
        violations = []
        instance_mutations = {"save", "delete"}
        manager_mutations = {
            "create",
            "get_or_create",
            "update_or_create",
            "bulk_create",
            "bulk_update",
        }

        for source_path in sorted(root.glob("*/views.py")):
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
        root = Path(__file__).resolve().parents[1]
        violations = []
        for source_path in sorted(root.glob("*/views.py")):
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


class StyleContractTests(SimpleTestCase):
    """The style bundle is a contract that extensions render against.

    These guard failures that are invisible in review and silent at runtime:
    CSS resolves an undefined custom property to an invalid value rather than
    erroring, so a typo'd token degrades to a browser default (a black SVG
    fill) instead of breaking loudly.
    """

    @staticmethod
    def _stylesheet() -> str:
        root = Path(__file__).resolve().parents[1]
        return (root / "static" / "css" / "app.css").read_text(encoding="utf-8")

    def test_every_referenced_custom_property_is_defined(self):
        import re

        css = self._stylesheet()
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.MULTILINE))
        referenced = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", css))
        # A fallback (var(--x, #fff)) is still a typo worth catching, so the
        # comparison deliberately ignores whether one was supplied.
        self.assertEqual(sorted(referenced - defined), [])

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

        import re
        import subprocess

        root = Path(__file__).resolve().parents[1]
        # Bound before the attempt: skipTest raises, so the loop below is
        # unreachable when git is absent -- but that is a fact about skipTest,
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
        reserved = re.compile(
            r"^(?:127\.|10\.|192\.168\.|169\.254\.|0\.|255\.|"
            r"172\.(?:1[6-9]|2[0-9]|3[01])\.|"
            r"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|"
            r"203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|"
            r"1\.1\.1\.1|8\.8\.8\.8)"
        )
        address = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
        # Long enough to be key material rather than a fixture. A real
        # ed25519 public key is 68 base64 characters; tests legitimately use
        # short stand-ins, and failing on those would teach people to weaken
        # the check rather than fix a leak.
        host_key = re.compile(r"ssh-(?:ed25519|rsa) AAAA[A-Za-z0-9+/]{32,}")
        # A hostname under the deployment's own private zone. Not secret --
        # nothing outside the network resolves it -- but it names one
        # installation's topology, and a public repository holds the shape
        # rather than the deployment. `example` and `invalid` are reserved for
        # writing about hostnames, which is what a fixture is doing.
        private_host = re.compile(
            r"\b[a-z0-9-]+\.(?!example\b|invalid\b|test\b|localhost\b)"
            r"(?:homelab|lan|internal|local)\b"
        )
        # Lockfiles and pinned action SHAs are hashes, not hosts.
        skip = ("package-lock.json", "requirements.txt", ".github/")

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
            for candidate in address.findall(text):
                octets = candidate.split(".")
                if any(int(part) > 255 for part in octets):
                    continue  # a version string, not an address
                if not reserved.match(candidate):
                    findings.append(f"{name}: {candidate}")
            if host_key.search(text):
                findings.append(f"{name}: ssh host key")
            for candidate in sorted(set(private_host.findall(text))):
                findings.append(f"{name}: {candidate}")

        self.assertEqual(findings, [], f"reachable endpoints in tracked files: {findings}")

    def test_images_live_where_images_belong(self):
        """A screenshot taken while debugging is not an asset of this project.

        A full-page capture taken while checking a rendered page was once
        committed to the root of this repository and referenced by nothing.
        This repository is public, so a capture of any internal page is
        published the moment it is pushed -- carrying whatever happened to be
        on screen -- and force-pushing afterwards does not unpublish it.

        Images are diagrams, documentation captures, or icons, and each of
        those has a home. Anything outside them is something that arrived by
        accident.

        The question is what is *committed*, not what is on the disk, so it is
        asked of git rather than of the filesystem. A working tree holds plenty
        of images that are nobody's business -- the Playwright MCP writes
        captures to `.playwright-mcp/`, which is ignored and therefore already
        safe -- and a walk of the disk would either report those or need a
        hand-maintained list of directories to skip. Tracked files are exactly
        the ones that can be pushed.
        """
        import subprocess

        root = Path(__file__).resolve().parents[1]
        allowed = ("docs/diagrams/", "docs/images/", "static/img/")
        suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        # This suite also runs inside the composed image, which is the source
        # tree without the checkout that produced it -- no .git, and no git
        # binary either. There is nothing to guard there: what is in the image
        # is already decided, and this asks what would be pushed. So it is
        # skipped rather than failed, and still runs everywhere the answer can
        # change.
        # Bound before the attempt: skipTest raises, so the read below is
        # unreachable when git is absent -- but that is a fact about skipTest,
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

        `plot_right` was added to the bar chart precisely so its template would
        stop drawing gridlines to 702 whatever the chart's own width was. The
        line chart's template was then written with 48, 702, 12 and 214 spelled
        out, so moving the plot moved the bars and the line but left that
        chart's gridlines and marks behind. Any literal that equals a plot
        coordinate is the same fault returning.
        """
        import re

        from .ui import PLOT_HEIGHT, PLOT_LEFT, PLOT_RIGHT, PLOT_TOP, PLOT_WIDTH

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
        root = Path(__file__).resolve().parents[1]
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
        offers roughly 284-446px. Every floor ever set here -- 620px, then
        480px -- was above that range, so it could not protect a narrow plot;
        it could only guarantee that every chart on the page scrolled at once.
        Label collision is handled by `Chart.dense` and a container query,
        which measure the labels and the card rather than guessing at a width.
        """
        import re

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
        import re

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
        import re

        css = self._stylesheet()
        slots = re.findall(r"^\s*(--series-\d+)\s*:\s*(#[0-9a-fA-F]{6})", css, re.MULTILINE)
        values = [value.lower() for _, value in slots]
        self.assertGreaterEqual(len(slots), 5, "expected at least 5 categorical slots")
        self.assertEqual(len(values), len(set(values)), "series slots must be distinct")

    def test_series_fills_use_categorical_slots_not_status_colours(self):
        import re

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

    An extension once invented a class name for a card it rendered; nothing
    errored and nothing was styled. The partials are the host's published UI
    contract, so anything they name has to exist here -- otherwise the first
    surface to adopt a primitive is the one that discovers it is unstyled.
    """

    def test_partial_classes_are_defined_in_the_stylesheet(self):
        import re

        root = Path(__file__).resolve().parents[1]
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

    A multi-line one is not a comment: it renders verbatim into the page. This
    shipped once, printing template source across the site navigation, and the
    failure is invisible in review because it looks exactly like a comment.
    """

    def test_no_multi_line_hash_comments_in_templates(self):
        import re

        root = Path(__file__).resolve().parents[1] / "templates"
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


class WorkflowSecrecyTests(SimpleTestCase):
    """A private value reaches a public log through the environment or not at all.

    Actions expands ``${{ … }}`` into the text of the step it then echoes, so a
    value interpolated into a script body is printed in full before the step
    runs -- ahead of anything the job does later to conceal it. Passed through
    ``env:`` it is a shell variable the echo never sees, and a secret is masked
    on top of that.
    """

    def workflows(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        return sorted(root.glob("*.yml"))

    def run_block_lines(self, text: str):
        """Every line of every ``run:`` script, with its line number.

        Read by indentation rather than parsed, so this needs no YAML library
        and cannot start disagreeing with one about what a block contains.

        A one-line ``run:`` counts. Checking only block scalars is how a
        ``docker login`` that piped a token straight into a shell went unnoticed
        by the check written to find exactly that.
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
            "pass it through env: instead — a script body is echoed verbatim",
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
    about a or b. ``assertGreater(a, b)`` prints both. CodeQL flags this and
    nothing local did, so it was found in review rather than before it.
    """

    SPECIFIC = {
        "Eq": "assertEqual", "NotEq": "assertNotEqual",
        "Lt": "assertLess", "LtE": "assertLessEqual",
        "Gt": "assertGreater", "GtE": "assertGreaterEqual",
        "Is": "assertIs", "IsNot": "assertIsNot",
        "In": "assertIn", "NotIn": "assertNotIn",
    }

    def test_no_assertion_hides_a_comparison_inside_a_boolean(self):
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in sorted(root.rglob("test*.py")) + sorted(root.rglob("tests.py")):
            if ".venv" in path.parts:
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
                    f"{path.relative_to(root)}:{node.lineno} — use {better}"
                )

        self.assertEqual(offenders, [])
