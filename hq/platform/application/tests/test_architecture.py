"""How the code is laid out: delivery adapters, file sizes, requests that never wait, where tests and dependencies live."""

import ast
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import brotli
import httpx2
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings, tag
from starlette.staticfiles import StaticFiles

from hq.platform.application.tables import TableListMixin
from hq.platform.core.compression import Compressed
from hq.platform.core.network import TrustedNetworkASGI
from hq.platform.core.static import CachedStaticFiles

from .source import source_files, template_files
from .test_view_writes import routed_modules, shown


class DeliveryAdapterArchitectureTests(SimpleTestCase):
    """Which module may import which is pyproject.toml's import contracts, held by lint:imports."""

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
        # Django's own stack, with one compressor around it and none inside,
        # so no answer is encoded twice or by the coding a client takes least.
        self.assertIsInstance(django_route.app, Compressed)
        self.assertIs(django_route.app.app, asgi.django_application)
        self.assertEqual([name for name in settings.MIDDLEWARE if "gzip" in name.lower()], [])

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
                    headers={"Accept-Encoding": "gzip, br"},
                )

        with TemporaryDirectory() as directory:
            Path(directory, "bundle.0123456789ab.css").write_text("a" * 2000, encoding="utf-8")
            compressed = brotli.compress(b"a" * 2000)
            Path(directory, "bundle.0123456789ab.css.br").write_bytes(compressed)
            manifest = SimpleNamespace(hashed_files={"bundle.css": "bundle.0123456789ab.css"})
            with patch("hq.platform.core.static.staticfiles_storage", manifest):
                response = async_to_sync(request)(directory)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "br")
        # The copy collectstatic wrote, byte for byte: nothing compressed it again.
        self.assertEqual(response.headers["content-length"], str(len(compressed)))
        self.assertEqual(response.text, "a" * 2000)
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    @tag("source", "tree")
    def test_paginated_list_views_use_the_shared_table_engine(self):
        root = Path(__file__).resolve().parents[4]
        modules = routed_modules(root)
        loaded = {
            Path(module.__file__).resolve(): module
            for module in list(sys.modules.values())
            if module and getattr(module, "__file__", None)
        }
        # A class declaring its page size, read as Python resolves it, so an engine reached through a mixin counts.
        paginated = {
            f"{shown(root, source_path)}:{name}": value
            for source_path in modules
            for name, value in vars(loaded[source_path]).items()
            if isinstance(value, type)
            and value.__module__ == loaded[source_path].__name__
            and "paginate_by" in vars(value)
        }

        self.assertTrue(paginated, "the walk found no paginated view")
        self.assertEqual([name for name, view in paginated.items() if not issubclass(view, TableListMixin)], [])


@tag("source", "tree")
class FileSizeTests(SimpleTestCase):
    """No source file is longer than the ceiling mise.toml declares.

    A file grows freely below it; one past it is split by responsibility, a
    test module by the behavior it holds. Every file ``SEVERINO_FILE_LINE_HELD``
    globs is held to it, and a Python test module like the code it tests. Migrations, generated
    code and vendored copies are not counted, nor a helper module under tests/.
    The ceiling and the exemptions are mise.toml's ``SEVERINO_FILE_LINE_*``, which
    the launch-time brief check reads too; with none declared the test fails.
    """

    ROOT = Path(__file__).resolve().parents[4]
    # The fewest files each declared glob's walk can find in a checkout or the image.
    FLOOR = {"**/*.py": 100, "**/*.go": 50, "**/*.js": 4, "**/*.css": 4}

    @staticmethod
    def _declared(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise AssertionError(f"{name} is not declared: run through mise, whose [env] holds the ceiling")
        return value

    def _counted(self, path: Path) -> bool:
        relative = path.relative_to(self.ROOT)
        counted = self._declared("SEVERINO_FILE_LINE_COUNTED").split()
        exempt = self._declared("SEVERINO_FILE_LINE_EXEMPT").split()
        if any(relative.full_match(glob) for glob in counted):
            return True
        return not any(relative.full_match(glob) for glob in exempt)

    def test_no_source_file_exceeds_the_ceiling(self):
        limit = int(self._declared("SEVERINO_FILE_LINE_LIMIT"))
        held = self._declared("SEVERINO_FILE_LINE_HELD").split()
        self.assertEqual(sorted(held), sorted(self.FLOOR), "every held glob needs a floor, and every floor a held glob")
        sizes = {}
        for pattern in held:
            floor = self.FLOOR[pattern]
            # rglob already walks every directory, so the glob's leading **/ is dropped.
            found = [path for path in source_files(self.ROOT, pattern.removeprefix("**/")) if self._counted(path)]
            self.assertGreater(len(found), floor, f"the {pattern} walk found almost nothing; its root is wrong")
            sizes |= {
                str(path.relative_to(self.ROOT)): len(path.read_text(encoding="utf-8").splitlines()) for path in found
            }
        over = {path: lines for path, lines in sizes.items() if lines > limit}
        self.assertEqual(over, {}, "split these by responsibility, a test module by the behavior it holds")


class RequestNeverWaitsTests(SimpleTestCase):
    """A request answers from what HQ holds. It never reaches a network,
    starts a process or sleeps: the interpreter refuses it (``core.outbound``),
    whichever library the call came through."""

    ROOT = Path(__file__).resolve().parents[4]
    # Where each declared exception is entered. An entry here is a place a
    # request may wait: adding one is a decision, with its reason in ``ALLOWED``
    # and its module in request-waits-only-where-listed's ignores.
    ENTERED = {
        "oidc": {"hq/platform/core/oidc.py"},
        "lookup": {"hq/platform/application/lookup.py"},
        "tailscaled": {"hq/platform/application/tailscaled.py"},
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
        """request-waits-only-where-listed (tools/lint/ast-grep) refuses
        allowed() in every other module; each listed one enters its own."""

        import yaml

        from hq.platform.core.outbound import ALLOWED

        listed = set().union(*self.ENTERED.values())
        rule = self.ROOT / "tools/lint/ast-grep/rules/request-waits-only-where-listed.yml"
        ignored = yaml.safe_load(rule.read_text(encoding="utf-8"))["ignores"]
        self.assertEqual({glob for glob in ignored if "*" not in glob}, listed)
        entered: dict[str, set[str]] = {}
        for relative in listed:
            for node in ast.walk(ast.parse((self.ROOT / relative).read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "allowed"):
                    continue
                argument = node.args[0] if node.args else None
                name = argument.value if isinstance(argument, ast.Constant) else "<not a literal>"
                entered.setdefault(str(name), set()).add(relative)

        self.assertEqual(entered, self.ENTERED)
        self.assertEqual(set(ALLOWED), set(self.ENTERED))
        for name, reason in ALLOWED.items():
            self.assertGreater(len(reason), 60, name)

    @tag("source", "tree")
    def test_only_a_job_leaves_the_request_that_started_it(self):
        leaving = {relative for relative, source in self.sources() if "off_request" in source}

        self.assertEqual(leaving, {"hq/platform/core/outbound.py", "hq/domains/jobs/runner.py"})

    @tag("source", "tree")
    def test_asked_for_work_is_followed_by_one_script_behavior(self):
        script = (self.ROOT / "static" / "js" / "ask.js").read_text(encoding="utf-8")
        templates = "\n".join(path.read_text(encoding="utf-8") for _name, path in template_files())

        # The controls that ask for work outside the request are all an ask.
        for partial in ("_ask.html", "_job_progress.html", "_visit_refresh.html"):
            self.assertIn("data-ask", (self.ROOT / "templates" / "partials" / partial).read_text(encoding="utf-8"))
        for retired in ("data-job=", "data-visit-refresh", "data-job-note", "data-visit-status"):
            self.assertNotIn(retired, script + templates)
        self.assertEqual(script.count("[data-ask][data-ask-status]"), 1)


@tag("source", "tree")
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
    """lint:deptry holds every dependency to host code's imports, less the
    per-rule ignores in pyproject.toml. deptry says nothing of an ignore that
    has gone stale, so this holds each to a finding it would report."""

    def test_every_finding_deptry_ignores_is_one_it_would_report(self):
        import json
        import subprocess
        import sys
        import tomllib

        root = Path(__file__).resolve().parents[4]
        ignored = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["deptry"]["per_rule_ignores"]
        with TemporaryDirectory() as scratch:
            report = Path(scratch) / "deptry.json"
            # A --per-rule-ignores replaces the table, so every rule runs with no ignore.
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "deptry",
                    "hq",
                    "hq_sdk",
                    "--per-rule-ignores",
                    "DEP001=",
                    "--json-output",
                    report,
                ],
                cwd=root,
                capture_output=True,
                check=False,
            )
            findings = json.loads(report.read_text())

        reported = {
            rule: sorted({each["module"] for each in findings if each["error"]["code"] == rule}) for rule in ignored
        }
        self.assertEqual(reported, {rule: sorted(modules) for rule, modules in ignored.items()})
