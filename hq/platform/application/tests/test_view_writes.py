"""A view makes a domain write only through the capability chokepoint.

Read from the source by ``writers.view_writes``, over every view module, so a
view added later is held without anybody listing it.
"""

import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from .source import source_files
from .writers import view_writes

ROOT = Path(settings.BASE_DIR)


def view_modules(root: Path) -> list[Path]:
    """Every module under ``hq`` whose name says it holds views, tests aside."""

    return [
        path
        for path in source_files(root / "hq", "*.py")
        if "views" in path.name and "tests" not in path.relative_to(root).parts
    ]


# A tree laid out as HQ's is, so each name resolves where the analysis looks.
PLANTED = {
    "hq/platform/core/audit.py": """
def record_event(**kwargs):
    AuditLog.objects.create(**kwargs)
""",
    "hq/platform/application/capabilities.py": """
from hq.example.services import save_thing


def execute_capability(name, payload, *, principal):
    return save_thing(payload)
""",
    "hq/example/services.py": """
from hq.platform.core.audit import record_event


def save_thing(payload):
    Thing(**payload).save()


def note_thing():
    record_event(message="looked")
""",
    "hq/example/views.py": """
from hq.example import services
from hq.platform.application.capabilities import execute_capability


class ThingView:
    def post(self, request):
        services.save_thing(request.POST)

    def get(self, request):
        services.note_thing()

    def put(self, request):
        execute_capability("example.thing.save", request.POST, principal=None)
""",
}


@tag("source", "tree")
class ViewWriteTests(SimpleTestCase):
    def test_no_view_writes_outside_the_chokepoint(self):
        modules = view_modules(ROOT)
        self.assertTrue(modules, "the walk found no view modules")

        found = view_writes(ROOT, modules)

        self.assertEqual(
            [f"{write.path}.{write.function} -> {write.service}" for write in found],
            [],
            "Route each through execute_capability, or move a GET's effect to a POST or a sweep.",
        )

    def test_a_planted_write_is_caught_and_an_audit_row_or_the_chokepoint_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in PLANTED.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body.lstrip(), encoding="utf-8")

            found = view_writes(root, view_modules(root))

        self.assertEqual(
            [(write.function, write.service) for write in found],
            [("ThingView.post", "hq.example.services.save_thing")],
        )
