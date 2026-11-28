"""An open page asks for its own readings: only those, only when due, only by POST."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory, ReadRequest
from hq.platform.core.testing import declare

from .. import visit_refresh
from ..asks import Standing
from ..cadence_testing import controller_arrived, controller_never_arrived
from ..record_errors import RecordNotFound
from ..security import Principal, cli_principal
from ..visit_refresh import Reads, VisitRefreshCommand, request_visit_refresh

SWEEPS = override_settings(
    SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=3600,
    SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
)
OLD = timedelta(hours=3)
# A kind whose reader calls a provider's API.
FAST = "adguard.rewrite"


def store(kind: str, *, age: timedelta = timedelta(0), attempted: timedelta | None = None) -> None:
    """A stored reading ``age`` old, last attempted ``attempted`` ago (its age by default)."""

    row, _created = ProviderInventory.objects.update_or_create(
        kind=kind, defaults={"records": [], "reachable": True, "observed_at": timezone.now() - age}
    )
    ProviderInventory.objects.filter(pk=row.pk).update(
        updated_at=timezone.now() - (age if attempted is None else attempted)
    )


def asked() -> set[str]:
    return set(ReadRequest.objects.values_list("kind", flat=True))


def page(*kinds: str):
    """One page, ``example``, assembled from ``kinds``."""

    return patch.dict(
        visit_refresh.SUBJECTS,
        {"example": lambda name: Reads(kinds) if name == "known" else None},
    )


# What a page is told when nothing is being read for it.
NOTHING_ASKED = {"ok": True, "standing": Standing().as_json(), "message": "", "requested": []}


@SWEEPS
class VisitRefreshTests(TestCase):
    def setUp(self):
        # A controller that arrived a moment ago.
        controller_arrived()

    def ask(self, name: str = "known", principal: Principal | None = None):
        return request_visit_refresh(VisitRefreshCommand("example", name), principal=principal or cli_principal())

    def test_a_page_hq_does_not_have_asks_for_nothing(self):
        store(FAST, age=OLD)

        with page(FAST):
            for subject, name in (("example", "unknown"), ("no-such-subject", "known")):
                with self.subTest(subject=subject), self.assertRaises(RecordNotFound):
                    request_visit_refresh(VisitRefreshCommand(subject, name), principal=cli_principal())
        self.assertEqual(asked(), set())

    def test_with_no_controller_arriving_nothing_is_asked_or_promised(self):
        store(FAST, age=OLD)

        controller_never_arrived()
        with page(FAST):
            result = self.ask()

        self.assertEqual(result, NOTHING_ASKED)
        self.assertEqual(asked(), set())

    def test_a_waiting_read_says_what_waits_and_says_so_when_no_controller_will_take_it(self):
        store(FAST, age=OLD)
        with page(FAST):
            self.ask()
            self.assertEqual(visit_refresh.visit_standing("example", "known").state, "queued")

            controller_arrived(ago=timedelta(hours=2))
            stalled = visit_refresh.visit_standing("example", "known")

        self.assertFalse(stalled.live)
        self.assertEqual(stalled.state, "failed")


class SubjectTests(TestCase):
    """What a real page is assembled from, looked up rather than told."""

    def test_a_machine_nobody_reported_is_no_page(self):
        self.assertIsNone(visit_refresh.reads_of("machine", "no-such-machine"))

    def test_a_service_is_read_through_the_kinds_its_declarations_are(self):
        declare("example-rewrite", "adguard.rewrite", {"domain": "app.example.test", "answer": "192.0.2.10"})

        self.assertEqual(visit_refresh.reads_of("service", "app.example.test").kinds, ("adguard.rewrite",))
