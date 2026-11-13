"""A view makes a domain write only through the capability chokepoint.

Read from the source by ``writers.view_writes``, over the module of every view
the URL configuration routes, the host's and the example plugin's, so a view
is held wherever it is defined and whatever its module is called.
"""

import sys
import tempfile
from importlib import import_module
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag
from django.urls import get_resolver

from hq.platform.application.tests.reads import reads
from hq.platform.core.test_runner import plugin_test_modules

from ..routes import routed
from .source import source_files
from .writers import DECLARED_SERVICE, DIRECT, view_writes

ROOT = Path(settings.BASE_DIR).resolve()
EXAMPLE_URLS = "tests.fixtures.example_hq_plugin.urls"


def installed_packages() -> list[Path]:
    """The directory of each installed extension's top-level package, wherever it is installed."""

    packages = {name.split(".")[0] for name in plugin_test_modules()}
    return sorted(Path(import_module(package).__file__).resolve().parent for package in packages)


def routed_modules(root: Path) -> list[Path]:
    """The file of every module a routed view or one of its bases is defined in,
    within this checkout or an installed extension.

    The host's routes and error handlers, each installed extension's, and the
    example plugin's routes, which the suite mounts only where a test composes
    it. A base counts as the view, since what it runs is the view's (a form
    mixin's ``form_valid``). A library's views (the SSO handshake's) are
    neither, and not this check's to hold.
    """

    views = [route.view for route in (*routed(), *routed(import_module(EXAMPLE_URLS).urlpatterns))]
    # And what answers an error, which the configuration names beside its routes.
    views += [get_resolver().resolve_error_handler(status) for status in (400, 403, 404, 500)]
    every = [found for view in views for found in (view.__mro__ if isinstance(view, type) else (view,))]
    defined = {
        Path(source).resolve()
        for found in every
        if (source := getattr(sys.modules[found.__module__], "__file__", None))
    }
    # This checkout's own files, so a virtualenv inside it is left out.
    held = set(source_files(root, "*.py")) | {
        path for package in installed_packages() for path in source_files(package, "*.py")
    }
    return sorted(defined & held)


def shown(root: Path, path: Path) -> str:
    """Where a routed module is, said from the checkout or from its extension's package."""

    package = next((package for package in installed_packages() if path.is_relative_to(package)), None)
    return str(path.relative_to(package.parent if package else root))


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
    "hq/platform/application/web_writes.py": """
from hq.platform.application.capabilities import execute_capability


def execute_posted(request, name, payload):
    return execute_capability(name, payload, principal=None)


def write_once(request, operation):
    Key(request).save()
    return operation()
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
from hq.platform.application.web_writes import execute_posted


class ThingView:
    def post(self, request):
        services.save_thing(request.POST)

    def get(self, request):
        services.note_thing()

    def put(self, request):
        execute_posted(request, "example.thing.save", request.POST)

    def patch(self, request):
        execute_capability("example.thing.save", request.POST, principal=None)

    def delete(self, request):
        Thing.objects.filter(pk=request.POST["pk"]).delete()
""",
}


# Form mixins as ``writes`` declares them: a write through the view's declared
# service or a record's save, run under the posted key or not.
PLANTED_FORMS = {
    "hq/platform/application/records.py": """
def save_form(form, *, principal):
    return form.save()
""",
    "hq/platform/application/writes.py": """
from hq.platform.application.records import save_form
from hq.platform.application.web_writes import write_once


class KeyedCreate:
    def form_valid(self, form):
        return write_once(self.request, lambda: self.service(form.cleaned_data))


class KeyedCommand:
    def run(self, command):
        return self.service(command)

    def form_valid(self, form):
        return write_once(self.request, lambda: self.run(form.cleaned_data))

    def form_invalid(self, form):
        return self.run(form.cleaned_data)


class BareCreate:
    def form_valid(self, form):
        return self.service(form.cleaned_data)


class BareRecord:
    def form_valid(self, form):
        return save_form(form, principal=None)
""",
    "hq/example/forms.py": """
from hq.example.services import save_thing
from hq.platform.application.web_writes import write_once


class ThingForm:
    def form_valid(self, form):
        return write_once(self.request, lambda: save_thing(form.cleaned_data))
""",
}


# An extension, installed outside the checkout, reaching the host through
# hq_sdk's re-exports: a capability, a form mixin's hook it overrides, an
# audit row, and a service of its own.
PLANTED_SDK = {
    "hq_sdk/audit.py": "from hq.platform.core.audit import record_event\n",
    "hq_sdk/capabilities.py": "from hq.platform.application.web_writes import execute_posted\n",
    "hq_sdk/writes.py": "from hq.platform.application.writes import KeyedCommand\n",
}
PLANTED_EXTENSION = {
    "example_extension/services.py": """
def save_other(payload):
    Other(**payload).save()
""",
    "example_extension/views.py": """
from hq_sdk import capabilities
from hq_sdk.audit import record_event
from hq_sdk.writes import KeyedCommand

from .services import save_other


class OtherView:
    def post(self, request):
        capabilities.execute_posted(request, "example.other.save", request.POST)
        record_event(message="saved")

    def put(self, request):
        save_other(request.POST)


class OtherForm(KeyedCommand):
    def run(self, command):
        return self._saved(command)

    def _saved(self, command):
        return save_other(command)
""",
}


def _planted(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.lstrip(), encoding="utf-8")


@tag("source", "tree")
@reads("**/*.py")
class ViewWriteTests(SimpleTestCase):
    def test_no_view_writes_outside_the_chokepoint(self):
        modules = routed_modules(ROOT)
        self.assertTrue(modules, "the walk found no routed views")

        found = view_writes(ROOT, modules, installed_packages())

        self.assertEqual(
            [f"{write.path}.{write.function} -> {write.service}" for write in found],
            [],
            "Route each through execute_posted (a form mixin's through write_once), "
            "or move a GET's effect to a POST or a sweep.",
        )

    def test_a_planted_write_is_caught_and_an_audit_row_or_the_chokepoint_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _planted(root, PLANTED)

            found = view_writes(root, [root / "hq/example/views.py"])

        self.assertEqual(
            [(write.function, write.service) for write in found],
            [
                ("ThingView.delete", DIRECT),
                ("ThingView.patch", "hq.platform.application.capabilities.execute_capability"),
                ("ThingView.post", "hq.example.services.save_thing"),
            ],
        )

    def test_a_form_mixin_writing_without_the_posted_key_is_caught_and_one_under_it_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _planted(root, {**PLANTED, **PLANTED_FORMS})

            found = view_writes(root, [root / "hq/platform/application/writes.py", root / "hq/example/forms.py"])

        self.assertEqual(
            sorted((write.path, write.function, write.service) for write in found),
            [
                # A view's own module keys nothing: its write goes through a capability.
                ("hq.example.forms", "ThingForm.form_valid", "hq.example.services.save_thing"),
                ("hq.example.forms", "ThingForm.form_valid", "hq.platform.application.web_writes.write_once"),
                ("hq.platform.application.writes", "BareCreate.form_valid", DECLARED_SERVICE),
                (
                    "hq.platform.application.writes",
                    "BareRecord.form_valid",
                    "hq.platform.application.records.save_form",
                ),
                # The hook a keyed lambda runs, called outside one.
                (
                    "hq.platform.application.writes",
                    "KeyedCommand.form_invalid",
                    "hq.platform.application.writes.KeyedCommand.run",
                ),
            ],
        )

    def test_an_installed_extension_is_read_through_hq_sdk_and_held_as_the_host_is(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "host")
            package = Path(directory, "site", "example_extension")
            _planted(root, {**PLANTED, **PLANTED_FORMS, **PLANTED_SDK})
            _planted(package.parent, PLANTED_EXTENSION)

            found = view_writes(root, [package / "views.py"], [package])

        self.assertEqual(
            [(write.path, write.function, write.service) for write in found],
            [("example_extension.views", "OtherView.put", "example_extension.services.save_other")],
        )
