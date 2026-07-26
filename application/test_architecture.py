from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase


class DeliveryAdapterArchitectureTests(SimpleTestCase):
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
