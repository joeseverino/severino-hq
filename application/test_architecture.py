from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
import httpx
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

from core.static import CachedStaticFiles


class DeliveryAdapterArchitectureTests(SimpleTestCase):
    def test_asgi_routes_static_assets_before_django(self):
        from config.asgi import application

        static_route, django_route = application.routes[-2:]
        self.assertEqual(static_route.path, "/static")
        self.assertIsInstance(static_route.app, GZipMiddleware)
        self.assertIsInstance(static_route.app.app, StaticFiles)
        self.assertIsInstance(static_route.app.app, CachedStaticFiles)
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
