from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase
from starlette.staticfiles import StaticFiles


class DeliveryAdapterArchitectureTests(SimpleTestCase):
    def test_asgi_routes_static_assets_before_django(self):
        from config.asgi import application

        static_route, django_route = application.routes[-2:]
        self.assertEqual(static_route.path, "/static")
        self.assertIsInstance(static_route.app, StaticFiles)
        self.assertEqual(django_route.path, "")

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
