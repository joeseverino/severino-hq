"""Every operation HQ binds has a title in words, so no audit line is headed
by its code ("Bridge Inventory") or a guess made from it."""

import tempfile
from importlib import import_module
from pathlib import Path
from textwrap import dedent

from django.test import SimpleTestCase, tag

from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.platform.application.domains import host_records
from hq.platform.application.resource_capabilities import LIFECYCLE_VERBS
from hq.platform.application.tests.apart import apart

from .operation_bindings import bindings

ROOT = Path(__file__).resolve().parents[4]
# A test process binds its own thread under this name; no deployed HQ writes a row in it.
NOT_DEPLOYED = {("hq/platform/core/test_runner.py", "test")}


def title_of(operation: str) -> str | None:
    # Imported where it runs, so this module imports at a base without it.
    from hq.platform.core.audit_words import title_of as titled

    return titled(operation)


def named_by(template: str) -> list[str]:
    """Every name an f-string binding can make, from the declaration its
    parts come from. A binding missing here fails: say where its names come from."""

    actions = [action.name for action in ACTIONS]
    records = list(host_records())
    families = {
        "f'bridge.{action.name}'": [f"bridge.{name}" for name in actions],
        "f'bridge.{action.name}.refused'": [f"bridge.{name}.refused" for name in actions],
        "f'bridge.{action}'": [f"bridge.{name}" for name in actions],
        "f'{records.noun}.{verb}'": [
            f"{entry.noun}.{verb}"
            for entry in records
            for verb in ("create", "update")
            if verb != "create" or entry.create
        ],
        "f'{records.noun}.delete'": [f"{entry.noun}.delete" for entry in records],
        "f'infrastructure.{action}.request'": [f"infrastructure.{verb}.request" for verb in sorted(LIFECYCLE_VERBS)],
    }
    return families[template]


def bound_names(found: list[list[str]]) -> dict[str, list[str]]:
    """Each operation name a binding in ``found`` makes, with where it is made."""

    names: dict[str, list[str]] = {}
    for path, line, kind, text in found:
        where = f"{path}:{line}"
        if kind == "literal":
            made = [text]
        elif kind == "fstring":
            made = named_by(text)
        else:
            value = getattr(import_module(path.removesuffix(".py").replace("/", ".")), text, None)
            # A parameter or a callable, which names nothing until it runs.
            made = [value] if isinstance(value, str) else []
        for name in made:
            if (path, name) not in NOT_DEPLOYED:
                names.setdefault(name, []).append(where)
    return names


@tag("source", "tree")
class OperationTitleTests(SimpleTestCase):
    def test_every_operation_this_checkout_binds_has_a_declared_title(self):
        names = bound_names(apart(bindings, ROOT))

        self.assertIn("bridge.report", names, "the walk read no controller binding, so it checks nothing")
        self.assertIn("approval.lapse", names, "the walk read no constant, so it checks nothing")
        self.assertEqual({name: where for name, where in names.items() if not title_of(name)}, {})

    def test_an_operation_nobody_titled_is_found(self):
        """The negative control: a walk that read nothing would pass the test above."""

        case = dedent(
            """
            from hq.platform.core.audit import audit_operation

            UNTITLED = "example.untitled.constant"


            def work(principal):
                with audit_operation(operation="example.untitled", principal=principal):
                    pass
                with audit_operation(operation=UNTITLED, principal=principal):
                    pass
            """
        )
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "example.py").write_text(case, encoding="utf-8")

            found = apart(bindings, folder)

        self.assertEqual(sorted(text for _path, _line, _kind, text in found), ["UNTITLED", "example.untitled"])
        self.assertIsNone(title_of("example.untitled"))


class TitleTests(SimpleTestCase):
    def test_a_controller_action_is_titled_by_its_declaration(self):
        for action in ACTIONS:
            with self.subTest(action=action.name):
                self.assertEqual(title_of(f"bridge.{action.name}"), action.title)
                self.assertTrue(title_of(f"bridge.{action.name}.refused"))

    def test_a_name_nothing_declares_has_no_title(self):
        for name in ("", "bridge.example-undeclared", "example.undeclared", "core:audit_list"):
            with self.subTest(name=name):
                self.assertIsNone(title_of(name))
