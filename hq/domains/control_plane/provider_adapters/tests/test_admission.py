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

        for kind, reason in (("adguard.rewrite", "Nothing reads it."), ("machine", "")):
            with self.subTest(kind), self.assertRaisesRegex(ValueError, "exactly one"):
                replace(PROVIDERS[kind], unobserved_reason=reason)


class ProviderReadsTests(SimpleTestCase):
    def test_every_kind_hq_keeps_is_read_by_a_provider_and_each_part_with_what_it_needs(self):
        from hq.domains.control_plane.providers import provider_reads
        from hq.platform.application.readings import KEPT

        every = [(provider, read) for provider, reads in provider_reads().items() for read in reads]
        listed = {(provider, read.kind, read.source): read for provider, read in every}
        # Nothing a provider reads is listed twice.
        self.assertEqual(len(listed), len(every))
        named = {read.kind for read in listed.values()}
        self.assertLessEqual({*OBSERVATIONS, *(kind for kind, spec in PROVIDERS.items() if spec.sweeps)}, named)
        self.assertFalse({kind for kind, spec in PROVIDERS.items() if not spec.sweeps} & named)
        self.assertLessEqual({kept.cadence for kept in KEPT if kept.through}, named)
        for kind, spec in PROVIDERS.items():
            for part in spec.parts:
                if part.provider and part.provider not in spec.connection_providers:
                    with self.subTest(kind=kind, part=part.name):
                        self.assertLessEqual(set(part.requires), set(listed[(part.provider, kind, "part")].permissions))
