"""Adding a provider is writing its modules and admitting it once.

Everything else HQ knows about a provider is gathered from what the admitted
module declares, so these tests hold the gathering rather than a list.
"""

from importlib import import_module
from pkgutil import iter_modules
from types import SimpleNamespace

from django.test import SimpleTestCase

from hq.domains.control_plane import observations
from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
from hq.domains.control_plane.connection_shapes import API_TOKEN, LOGIN
from hq.domains.control_plane.observations.contract import ObservationRecord, ObservationSpec, registry
from hq.domains.control_plane.provider_adapters import (
    ADMITTED,
    CONNECTIONS,
    DECLARATIONS,
    admitted_connections,
    undeclared_connections,
)
from hq.domains.control_plane.provider_spec import ConnectionKind

EXAMPLE = ConnectionKind("Example", "scoped", API_TOKEN)


class ConnectionAdmissionTests(SimpleTestCase):
    def test_an_admitted_module_brings_its_connection(self):
        module = SimpleNamespace(CONNECTIONS={"example_api": EXAMPLE})

        found = admitted_connections((*ADMITTED, module))

        self.assertIs(found["example_api"], EXAMPLE)
        self.assertEqual(list(found)[: len(CONNECTIONS)], list(CONNECTIONS))

    def test_a_module_outside_admission_brings_nothing(self):
        self.assertNotIn("example_api", CONNECTIONS)

    def test_one_provider_is_declared_once(self):
        first = SimpleNamespace(CONNECTIONS={"example_api": EXAMPLE})
        second = SimpleNamespace(CONNECTIONS={"example_api": ConnectionKind("Other", "coarse", LOGIN)})

        with self.assertRaisesRegex(ValueError, "example_api"):
            admitted_connections((first, second))

    def test_a_kind_naming_an_undeclared_connection_is_found(self):
        kind = SimpleNamespace(connection_providers=("example_api",))

        self.assertEqual(undeclared_connections((kind,), CONNECTIONS), ["example_api"])

    def test_every_admitted_kind_names_only_declared_connections(self):
        self.assertEqual(undeclared_connections(DECLARATIONS, CONNECTIONS), [])

    def test_the_connection_table_is_what_the_modules_declare(self):
        self.assertEqual(dict(CONNECTION_KINDS), CONNECTIONS)


class ObservationDiscoveryTests(SimpleTestCase):
    def test_every_readings_module_is_registered(self):
        modules = [
            import_module(f"hq.domains.control_plane.observations.{info.name}")
            for info in iter_modules(observations.__path__)
            if info.name != "contract" and not info.name.startswith(("_", "test"))
        ]
        declared = [spec.kind for module in modules for spec in module.OBSERVATIONS]

        self.assertTrue(modules)
        self.assertEqual(sorted(observations.OBSERVATIONS), sorted(declared))

    def test_the_registry_order_is_the_module_names(self):
        providers = [spec.provider for spec in observations.OBSERVATIONS.values()]
        modules = [module.__name__.rsplit(".", 1)[1] for module in observations._SPEC_MODULES]

        self.assertEqual(modules, sorted(modules))
        self.assertEqual(len(providers), len(observations.OBSERVATIONS))

    def test_a_reading_that_connects_must_name_what_it_connects(self):
        spec = ObservationSpec(
            "example.group",
            "example",
            "Example group",
            ObservationRecord,
            connects=lambda record: True,
        )

        with self.assertRaisesMessage(ValueError, "connects needs the containers"):
            registry((spec,))
