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
