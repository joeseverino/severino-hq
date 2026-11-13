from typing import Any, get_args

from django.test import SimpleTestCase
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hq.platform.application.automation_manifest import (
    DuplicateOperation,
    OperationManifest,
    OperationRegistry,
    UnknownOperation,
)
from hq.platform.application.contracts import EFFECTS
from hq_sdk.capabilities import StrictCommand


class ServiceLogs(StrictCommand):
    service: str
    lines: int = 200


class Window(StrictCommand):
    hours: int


class Nested(StrictCommand):
    window: Window


def declared(**changes: Any) -> dict[str, Any]:
    return {
        "id": "example.service_logs",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "playbook": "playbooks/service_logs.yml",
        "input_type": ServiceLogs,
        "target_kinds": ("example.machine",),
        "hosts": ("app.example.com",),
        "privilege": None,
        "effect": "read",
        "timeout_seconds": 120,
        "preconditions": ("The service is installed on the host.",),
        "verification": ("The answer holds the service's state and its last lines.",),
        "recovery": ("Nothing to recover: the procedure changes nothing.",),
        "retry": "safe",
        **changes,
    }


def manifest(**changes: Any) -> OperationManifest:
    return OperationManifest(**declared(**changes))


class DeclarationTests(SimpleTestCase):
    def test_a_complete_manifest_is_declared(self) -> None:
        built = manifest(
            id="example.package_upgrade",
            effect="infrastructure_change",
            privilege="example.package_maintenance",
            input_type=Nested,
            retry="observe_first",
        )

        self.assertEqual(built.id, "example.package_upgrade")
        self.assertEqual(built.privilege, "example.package_maintenance")
        self.assertIs(built.input_type, Nested)

    def test_a_playbook_is_a_path_inside_the_project(self) -> None:
        for playbook in ("site.yml", "playbooks/service_logs.yml", "playbooks/linux/disk-report.v2.yml"):
            with self.subTest(playbook):
                self.assertEqual(manifest(playbook=playbook).playbook, playbook)

    def test_it_cannot_change_once_declared(self) -> None:
        built = manifest()

        with self.assertRaises(ValidationError):
            built.timeout_seconds = 9999  # type: ignore[misc]  # the assignment is the refusal under test

    def test_its_effects_are_capability_effects(self) -> None:
        effects = get_args(OperationManifest.model_fields["effect"].annotation)

        self.assertLessEqual(set(effects), EFFECTS)

    def test_targets_and_hosts_read_in_one_order(self) -> None:
        first = manifest(target_kinds=("example.vm", "example.machine"), hosts=("b.example.com", "a.example.com"))
        second = manifest(target_kinds=("example.machine", "example.vm"), hosts=("a.example.com", "b.example.com"))

        self.assertEqual(first.model_dump(exclude={"input_type"}), second.model_dump(exclude={"input_type"}))


class InvalidShapeTests(SimpleTestCase):
    def assertRefused(self, **changes: Any) -> None:
        with self.assertRaises(ValidationError):
            manifest(**changes)

    def test_an_unknown_field(self) -> None:
        self.assertRefused(inventory="inventory.yml")

    def test_a_bad_id(self) -> None:
        for bad in ("Example.Logs", "example..logs", "example.logs-tail", "", ".example"):
            with self.subTest(bad):
                self.assertRefused(id=bad)

    def test_a_revision_that_is_not_a_full_commit(self) -> None:
        for bad in ("0123456", "0123456789ABCDEF0123456789ABCDEF01234567", "g" * 40, "0" * 41, "main"):
            with self.subTest(bad):
                self.assertRefused(revision=bad)

    def test_a_playbook_outside_the_project_or_not_a_playbook(self) -> None:
        for bad in (
            "",
            "/srv/ansible/site.yml",
            "../site.yml",
            "playbooks/../../site.yml",
            "./site.yml",
            "playbooks//site.yml",
            "playbooks/.hidden.yml",
            "-e@vars.yml",
            "site.yaml",
            "site",
            ".yml",
            "playbooks/",
            "playbooks\\site.yml",
            "~/site.yml",
            "play books/site.yml",
            f"{'p' * 200}.yml",
        ):
            with self.subTest(bad):
                self.assertRefused(playbook=bad)

    def test_a_playbook_must_be_said(self) -> None:
        with self.assertRaises(ValidationError):
            OperationManifest(**{key: value for key, value in declared().items() if key != "playbook"})

    def test_a_missing_or_empty_statement(self) -> None:
        for field in ("preconditions", "verification", "recovery"):
            with self.subTest(field):
                with self.assertRaises(ValidationError):
                    OperationManifest(**{key: value for key, value in declared().items() if key != field})
                self.assertRefused(**{field: ("   ",)})
        self.assertRefused(recovery=())

    def test_a_change_states_what_it_needs_and_what_shows_it_worked(self) -> None:
        for field in ("preconditions", "verification"):
            with self.subTest(field):
                self.assertRefused(effect="infrastructure_change", **{field: ()})

    def test_a_read_may_state_neither(self) -> None:
        built = manifest(preconditions=(), verification=())

        self.assertEqual((built.preconditions, built.verification), ((), ()))

    def test_a_timeout_out_of_bounds(self) -> None:
        for bad in (0, -1, 3601, True):
            with self.subTest(bad):
                self.assertRefused(timeout_seconds=bad)

    def test_no_target_kind(self) -> None:
        self.assertRefused(target_kinds=())

    def test_a_host_that_is_not_a_name(self) -> None:
        for bad in ("App.example.com", "app.example.com.", "192.0.2.10", "-app.example.com"):
            with self.subTest(bad):
                self.assertRefused(hosts=(bad,))

    def test_an_unknown_effect_or_retry(self) -> None:
        self.assertRefused(effect="remote_write")
        self.assertRefused(retry="always")

    def test_privilege_must_be_said(self) -> None:
        with self.assertRaises(ValidationError):
            OperationManifest(**{key: value for key, value in declared().items() if key != "privilege"})


class InputTypeTests(SimpleTestCase):
    def test_an_input_that_takes_undeclared_fields(self) -> None:
        class Open(BaseModel):
            service: str

        with self.assertRaises(ValidationError):
            manifest(input_type=Open)

    def test_an_input_that_is_not_a_model(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(input_type=dict)

    def test_an_execution_field_however_it_is_spelled(self) -> None:
        class PlaybookPath(StrictCommand):
            playbook_path: str

        class Inventory(StrictCommand):
            inventory: str

        class ExtraVars(StrictCommand):
            extra: str = Field(alias="extra-vars")

        class Limit(StrictCommand):
            limit: str

        for model in (PlaybookPath, Inventory, ExtraVars, Limit):
            with self.subTest(model.__name__), self.assertRaises(ValidationError):
                manifest(input_type=model)

    def test_an_execution_field_in_a_nested_input(self) -> None:
        class Vars(StrictCommand):
            extravars: str

        class Outer(StrictCommand):
            options: list[Vars]

        with self.assertRaises(ValidationError):
            manifest(input_type=Outer)

    def test_a_nested_input_that_takes_undeclared_fields(self) -> None:
        class Loose(BaseModel):
            model_config = ConfigDict(extra="allow")

        class Outer(StrictCommand):
            options: Loose | None = None

        with self.assertRaises(ValidationError):
            manifest(input_type=Outer)

    def test_a_free_form_field(self) -> None:
        class Bag(StrictCommand):
            values: dict[str, str]

        class Anything(StrictCommand):
            value: Any

        for model in (Bag, Anything):
            with self.subTest(model.__name__), self.assertRaises(ValidationError):
                manifest(input_type=model)


class DenialTests(SimpleTestCase):
    def test_a_read_with_a_privilege_profile(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(effect="read", privilege="example.package_maintenance")

    def test_a_target_the_manifest_does_not_name(self) -> None:
        built = manifest()

        self.assertTrue(built.admits("example.machine", "app.example.com"))
        self.assertFalse(built.admits("example.vm", "app.example.com"))
        self.assertFalse(built.admits("example.machine", "db.example.com"))

    def test_no_hosts_admits_any_host_of_its_kinds(self) -> None:
        built = manifest(hosts=())

        self.assertTrue(built.admits("example.machine", "db.example.com"))
        self.assertFalse(built.admits("example.vm", "db.example.com"))


class RegistryTests(SimpleTestCase):
    def test_it_answers_a_manifest_by_id(self) -> None:
        logs = manifest()
        upgrade = manifest(id="example.package_upgrade", effect="infrastructure_change", retry="never")

        registry = OperationRegistry((logs, upgrade))

        self.assertIs(registry["example.package_upgrade"], upgrade)
        self.assertEqual(set(registry), {"example.service_logs", "example.package_upgrade"})

    def test_it_refuses_a_duplicate_id(self) -> None:
        with self.assertRaises(DuplicateOperation):
            OperationRegistry((manifest(), manifest(timeout_seconds=60)))

    def test_an_unknown_id(self) -> None:
        registry = OperationRegistry((manifest(),))

        with self.assertRaises(UnknownOperation):
            registry["example.missing"]
        self.assertIsNone(registry.get("example.missing"))

    def test_it_is_supplied_manifests_and_nothing_else(self) -> None:
        with self.assertRaises(TypeError):
            OperationRegistry([manifest()])  # type: ignore[arg-type]  # a list is the refusal under test
        with self.assertRaises(TypeError):
            OperationRegistry((declared(),))  # type: ignore[arg-type]  # a mapping is the refusal under test
        with self.assertRaises(TypeError):
            OperationRegistry("example.service_logs")  # type: ignore[arg-type]  # a path is the refusal under test
