"""Every command is refused at the capability runner, before anything is
written, to a caller that does not hold what it requires: a grant it lacks, an
interface the capability is never held at, or no person behind a token. The
refusal is recorded as a denial, so a handler may leave its authority to the
runner."""

from django.test import TestCase

from hq.platform.core.models import AuditLog, Revision
from hq.platform.search_index.models import SearchDocument

from ..capabilities import capability_registry, execute_capability
from ..integration_specs import CapabilitySpec
from ..security import AGENT_SURFACES, INTERNAL_INTERFACE, MCP_INTERFACE, Principal, operator_capabilities
from .procedures import Supply, manifest


def _written() -> dict[str, int]:
    """How many times each table has been written, as the database counts it."""

    return dict(Revision.objects.values_list("name", "value"))


class RefusedAtTheRunnerTests(TestCase):
    def setUp(self):
        # A procedure supplied, so the command each run is asked through is walked too.
        Supply(self, manifest())

    def test_a_caller_without_a_required_capability_is_refused_and_nothing_is_written(self):
        # In the browser and as an agent a person signed in, whose call
        # policy could otherwise hold for approval.
        held = operator_capabilities()
        for spec in capability_registry():
            for capability in spec.required_capabilities:
                for interface in ("web", MCP_INTERFACE):
                    lacking = Principal(
                        f"example-{interface}", interface, held - {capability}, person_key="example-key"
                    )
                    with self.subTest(capability=spec.name, requires=str(capability), interface=interface):
                        self._refused(spec, lacking, str(capability))

    def test_a_caller_granted_everything_is_refused_what_its_interface_or_its_person_can_never_hold(self):
        # Every grant there is, so only the interface and whether a person
        # signed the token in keep a capability out (security.HELD_ONLY_AT,
        # NEEDS_A_PERSON).
        held = operator_capabilities()
        callers = (
            *(Principal(f"example-{each}", each, held) for each in ("web", "cli", INTERNAL_INTERFACE, *AGENT_SURFACES)),
            *(Principal(f"example-{surface}", surface, held, person_key="example-key") for surface in AGENT_SURFACES),
        )
        refused = 0
        for spec in capability_registry():
            for caller in callers:
                if spec.permitted_to(caller):
                    continue
                refused += 1
                with self.subTest(capability=spec.name, interface=caller.interface, person=caller.person_key):
                    self._refused(spec, caller, *map(str, spec.required_capabilities))
        self.assertTrue(refused)

    def _refused(self, spec: CapabilitySpec, caller: Principal, *named: str) -> None:
        """Refused as ``forbidden`` naming one of ``named``, with a denial recorded and nothing else written."""

        before = _written()
        audited = AuditLog.objects.count()

        result = execute_capability(
            spec.name, {}, principal=caller, target="example-target" if spec.target_kind else None
        )

        self.assertEqual(result["error"]["code"], "forbidden", result)
        self.assertTrue(any(name in result["error"]["message"] for name in named), result)
        after = _written()
        moved = {table for table in after.keys() | before.keys() if after.get(table) != before.get(table)}
        # The refusal itself is recorded, and indexed for search, and nothing else.
        self.assertLessEqual(moved, {AuditLog._meta.db_table, SearchDocument._meta.db_table})
        self.assertEqual(
            set(AuditLog.objects.order_by("pk")[audited:].values_list("action", flat=True)),
            {AuditLog.Action.DENIED},
        )
