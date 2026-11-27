"""Adding a provider is writing its modules and admitting it once.

Everything else HQ knows about a provider is gathered from what the admitted
module declares, so these tests hold the gathering rather than a list.
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from hq.domains.control_plane import provider_adapters as admission, provider_spec
from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
from hq.domains.control_plane.connection_shapes import API_TOKEN, LOGIN
from hq.domains.control_plane.provider_adapters import (
    ADMITTED,
    CONNECTIONS,
    DECLARATIONS,
    OBSERVATIONS,
    undeclared_connections,
)
from hq.domains.control_plane.provider_spec import ConnectionKind
from hq.domains.control_plane.providers import PROVIDERS

EXAMPLE = ConnectionKind("Example", "scoped", API_TOKEN)


def events(name: str = "example.changed"):
    """A synthetic provider's events, each making an example reading due."""

    return provider_spec.InboundSpec({name: ("example.thing",)})


class ConnectionAdmissionTests(SimpleTestCase):
    def test_an_admitted_module_brings_its_connection(self):
        module = SimpleNamespace(CONNECTIONS={"example_api": EXAMPLE})

        found = admission.admitted((*ADMITTED, module), "CONNECTIONS")

        self.assertIs(found["example_api"], EXAMPLE)
        self.assertEqual(list(found)[: len(CONNECTIONS)], list(CONNECTIONS))

    def test_a_module_outside_admission_brings_nothing(self):
        self.assertNotIn("example_api", CONNECTIONS)

    def test_one_provider_is_declared_once(self):
        first = SimpleNamespace(CONNECTIONS={"example_api": EXAMPLE})
        second = SimpleNamespace(CONNECTIONS={"example_api": ConnectionKind("Other", "coarse", LOGIN)})

        with self.assertRaisesRegex(ValueError, "example_api"):
            admission.admitted((first, second), "CONNECTIONS")

    def test_a_kind_naming_an_undeclared_connection_is_found(self):
        kind = SimpleNamespace(connection_providers=("example_api",))

        self.assertEqual(undeclared_connections((kind,), CONNECTIONS), ["example_api"])

    def test_every_admitted_kind_names_only_declared_connections(self):
        self.assertEqual(undeclared_connections(DECLARATIONS, CONNECTIONS), [])

    def test_the_connection_table_is_what_the_modules_declare(self):
        self.assertEqual(dict(CONNECTION_KINDS), CONNECTIONS)


class InboundAdmissionTests(SimpleTestCase):
    def test_an_admitted_module_brings_its_events(self):
        declared = events()
        module = SimpleNamespace(INBOUND={"example_api": declared})

        found = admission.admitted((*ADMITTED, module), "INBOUND")

        self.assertIs(found["example_api"], declared)
        self.assertNotIn("example_api", admission.INBOUND)

    def test_one_providers_events_are_declared_once(self):
        first = SimpleNamespace(INBOUND={"example_api": events()})
        second = SimpleNamespace(INBOUND={"example_api": events("example.other")})

        with self.assertRaisesRegex(ValueError, "example_api"):
            admission.admitted((first, second), "INBOUND")

    def test_every_event_makes_due_a_reading_hq_stores_or_reads_itself(self):
        from hq.platform.application.outbound_work import reader_of

        for provider, spec in admission.INBOUND.items():
            for event, kinds in spec.events.items():
                with self.subTest(provider=provider, event=event):
                    self.assertTrue(
                        all(kind in OBSERVATIONS or kind in PROVIDERS or reader_of(kind) for kind in kinds), kinds
                    )


class ObservedTests(SimpleTestCase):
    def test_a_kind_is_swept_or_says_why_not_and_never_both(self):
        from dataclasses import replace

        swept, unswept = PROVIDERS["adguard.rewrite"], PROVIDERS["machine"]
        for case in (
            {"spec": swept, "unobserved_reason": "Nothing reads it."},
            {"spec": unswept, "unobserved_reason": ""},
        ):
            with self.subTest(case["spec"].kind), self.assertRaisesRegex(ValueError, "exactly one"):
                replace(case["spec"], unobserved_reason=case["unobserved_reason"])
