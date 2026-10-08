"""An open page asks for its own readings: only those, only when due, only by POST."""

import re
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderConnection, ProviderInventory, ReadRequest

from .. import visit_refresh
from ..asks import Standing, read_standing, watched
from ..freshness import VISIT_EVERY
from ..security import Capability, Principal, cli_principal
from ..visit_refresh import Reads, askable, request_visit_refresh

DIRECTORY = Path(tempfile.mkdtemp())
(DIRECTORY / "heartbeat").touch()
MARKERS = override_settings(
    SEVERINO_CONTROLLER_DOORBELL=str(DIRECTORY / "doorbell"),
    SEVERINO_ACTIVITY_MARKER=str(DIRECTORY / "activity"),
    # A controller that arrived a moment ago.
    SEVERINO_CONTROLLER_HEARTBEAT=str(DIRECTORY / "heartbeat"),
    SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=3600,
    SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
)
OLD = timedelta(hours=3)
# Kinds whose readers call a provider's API, and ones whose readers log in.
FAST = "adguard.rewrite"
LOGS_IN = ("host.perimeter", "caddy.route")


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
NOTHING_ASKED = {**Standing().as_json(), "requested": [], "status": ""}


def followed(status: str) -> Standing:
    """How the read behind a status address stands, as its resource would say."""

    from ..timestamps import moment

    found = watched(status.partition("watch=")[2])
    return read_standing(found["read"], moment(found["asked"]), connection_ref=found["ref"])


@MARKERS
class VisitRefreshTests(TestCase):
    def ask(self, name: str = "known", principal: Principal | None = None):
        return request_visit_refresh("example", name, principal=principal or cli_principal())

    def test_a_reading_older_than_its_cadence_is_asked_for(self):
        store(FAST, age=OLD)

        with page(FAST), self.captureOnCommitCallbacks(execute=True) as rung:
            result = self.ask()

        self.assertEqual(result["requested"], [FAST])
        self.assertTrue(result["live"])
        self.assertEqual(asked(), {FAST})
        self.assertEqual(len(rung), 1)

    def test_a_fresh_reading_is_left_alone_however_often_the_page_is_opened(self):
        store(FAST, age=VISIT_EVERY - timedelta(seconds=5))

        with page(FAST), self.captureOnCommitCallbacks(execute=True) as rung:
            results = [self.ask() for _ in range(5)]

        self.assertTrue(all(result["requested"] == [] and not result["live"] for result in results))
        self.assertEqual(asked(), set())
        self.assertEqual(rung, [])

    def test_reloading_while_a_read_is_pending_does_not_ask_again(self):
        store(FAST, age=OLD)

        with page(FAST):
            first, second = self.ask(), self.ask()

        self.assertEqual(first["requested"], [FAST])
        self.assertEqual(second["requested"], [])
        self.assertTrue(second["live"])
        self.assertEqual(ReadRequest.objects.count(), 1)

    def test_several_kinds_are_one_ask_and_one_doorbell(self):
        kinds = (FAST, "npm.proxy_host", "tailscale.device")
        for kind in kinds:
            store(kind, age=OLD)

        with page(*kinds), self.captureOnCommitCallbacks(execute=True) as rung:
            result = self.ask()

        self.assertEqual(sorted(result["requested"]), sorted(kinds))
        self.assertEqual(len(rung), 1)

    def test_only_what_the_page_is_made_of_is_asked_for(self):
        for kind in (FAST, "npm.proxy_host", "tailscale.device"):
            store(kind, age=OLD)

        with page(FAST):
            self.ask()

        self.assertEqual(asked(), {FAST})

    def test_a_reading_that_keeps_failing_is_not_asked_for_again_and_again(self):
        """A failed read stores nothing new, so its last good reading stays old.

        Judged by that, the kind would be due again the moment it had been
        asked: the page would reload, ask, be told it was answered, and reload,
        for as long as it stayed open, at a provider that is already refusing.
        """

        store(FAST, age=OLD, attempted=timedelta(seconds=10))

        with page(FAST):
            results = [self.ask() for _ in range(3)]

        self.assertTrue(all(result["requested"] == [] for result in results))
        self.assertEqual(asked(), set())

    def test_a_failing_reading_is_tried_again_once_its_cadence_has_passed(self):
        store(FAST, age=OLD, attempted=VISIT_EVERY + timedelta(seconds=5))

        with page(FAST):
            self.assertEqual(self.ask()["requested"], [FAST])

    def test_opening_a_page_never_asks_for_a_kind_whose_reader_logs_in(self):
        for kind in LOGS_IN:
            store(kind, age=timedelta(days=30))

        with page(*LOGS_IN):
            result = self.ask()

        self.assertEqual(result["requested"], [])
        self.assertEqual(asked(), set())

    def test_nothing_is_asked_for_that_could_not_be_answered(self):
        """A request nothing answers forces sweeps until it expires."""

        store("tls.certificate", age=OLD)   # declared, and no sweep reads it
        store("registry.domain", age=OLD)   # read by HQ, not the controller
        store("a.kind.nothing.declares", age=OLD)

        with page("tls.certificate", "registry.domain", "a.kind.nothing.declares", "npm.proxy_host"):
            result = self.ask()

        # And npm.proxy_host was never stored, so nothing has shown it is read.
        self.assertEqual(result, NOTHING_ASKED)
        self.assertEqual(asked(), set())

    def test_a_page_hq_does_not_have_asks_for_nothing(self):
        store(FAST, age=OLD)

        with page(FAST):
            self.assertIsNone(self.ask("unknown"))
        self.assertIsNone(request_visit_refresh("no-such-subject", "known", principal=cli_principal()))
        self.assertEqual(asked(), set())

    def test_somebody_who_could_not_press_read_now_reads_nothing_and_learns_nothing(self):
        store(FAST, age=OLD)
        ReadRequest.objects.create(kind=FAST)
        reader = Principal(interface="web", actor="reader", capabilities=frozenset({Capability.READ}))

        with page(FAST):
            result = self.ask(principal=reader)

        self.assertEqual(result, NOTHING_ASKED)

    def test_with_no_controller_arriving_nothing_is_asked_or_promised(self):
        store(FAST, age=OLD)

        with (
            override_settings(SEVERINO_CONTROLLER_HEARTBEAT=str(DIRECTORY / "never-written")),
            page(FAST),
        ):
            result = self.ask()

        self.assertEqual(result, NOTHING_ASKED)
        self.assertEqual(asked(), set())

    def test_the_page_is_told_what_to_watch_for_and_when_it_has_arrived(self):
        store(FAST, age=OLD)

        with page(FAST):
            status = self.ask()["status"]

        self.assertTrue(followed(status).live)
        # The controller reads it, successfully or not: either is an answer.
        ProviderInventory.objects.filter(kind=FAST).update(updated_at=timezone.now() + timedelta(seconds=1))
        self.assertFalse(followed(status).live)
        # And once answered the page is not due again, so it cannot reload twice.
        store(FAST, age=OLD, attempted=timedelta(0))
        with page(FAST):
            self.assertEqual(self.ask()["requested"], [])

    def test_a_watch_hq_did_not_sign_is_not_honoured(self):
        from ..asks import _WATCH_SALT

        forged = signing.dumps({"read": [FAST]}, salt="some.other.salt")

        for watch in ("", "not-a-token", forged, signing.dumps("a string", salt=_WATCH_SALT)):
            with self.subTest(watch=watch[:20]):
                self.assertIsNone(watched(watch))

    def test_what_asking_costs_grows_with_the_kinds_asked_for_and_no_faster(self):
        """Deciding is a fixed handful of queries, the last of them saying how
        the read stands. What grows is one audited write per kind asked for;
        nothing is worked out again for each."""

        few, many = (FAST,), (FAST, "npm.proxy_host", "tailscale.device", "adguard.dns", "npm.redirect")
        for kind in many:
            store(kind, age=OLD)
        decide, per_write = 8, 9

        with page(*few), self.assertNumQueries(decide + per_write * len(few)):
            self.ask()
        ReadRequest.objects.all().delete()
        with page(*many), self.assertNumQueries(decide + per_write * len(many)):
            self.ask()

    def test_asking_when_nothing_is_due_writes_nothing(self):
        store(FAST)

        with page(FAST), self.assertNumQueries(5):
            self.ask()


class AskableTests(TestCase):
    """Which kinds an open page may ask for, held against the real registries."""

    def test_a_provider_api_kind_is_and_one_read_by_logging_in_is_not(self):
        self.assertTrue(askable(FAST))
        self.assertTrue(askable("github.repository"))
        for kind in (*LOGS_IN, "tls.certificate", "registry.domain", "a.kind.nothing.declares"):
            with self.subTest(kind=kind):
                self.assertFalse(askable(kind))

    def test_only_the_known_readers_open_a_shell(self):
        """``askable`` trusts what a kind declares: read through "ssh" or not.

        That holds only while the readers that log in are the ones declared to.
        A module that starts running a shell command on a machine fails here,
        so whoever adds it decides how its kinds are asked for, rather than
        finding out from a host's login records.
        """

        root = Path(__file__).resolve().parents[4]
        opens_a_shell = re.compile(r"\.SSH\(ctx")
        found = sorted(
            str(path.relative_to(root))
            for path in (root / "controller" / "providers").glob("*.go")
            if not path.name.endswith("_test.go")
            and opens_a_shell.search(path.read_text(encoding="utf-8"))
        )

        self.assertEqual(
            found,
            [
                "controller/providers/caddy.go",
                "controller/providers/controller.go",
                "controller/providers/glance.go",
                "controller/providers/host_readings.go",
                "controller/providers/tls.go",
            ],
        )


@MARKERS
class SubjectTests(TestCase):
    """What a real page is assembled from, looked up rather than told."""

    def test_a_machine_nobody_reported_is_no_page(self):
        self.assertIsNone(visit_refresh.reads_of("machine", "no-such-machine"))

    def test_a_hostname_with_nothing_declared_has_a_page_and_nothing_to_read(self):
        self.assertEqual(visit_refresh.reads_of("service", "example.test"), Reads())

    def test_a_service_is_read_through_the_kinds_its_declarations_are(self):
        ManagedResource.objects.create(
            key="example-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"},
        )

        self.assertEqual(visit_refresh.reads_of("service", "app.example.test").kinds, ("adguard.rewrite",))

    def test_a_machine_is_read_through_what_its_connections_read(self):
        from ..credential_sight import fed_kinds
        from ..machines import Machine

        ProviderConnection.objects.create(
            controller_id="example-controller", connection_ref="example-tailnet",
            provider="tailscale", observed_at=timezone.now(),
        )
        ProviderConnection.objects.create(
            controller_id="example-controller", connection_ref="example-shell",
            provider="ssh", observed_at=timezone.now(),
        )
        found = Machine(name="example-host", reached_by=("example-tailnet", "example-shell"))

        with patch("hq.platform.application.machines.machine", return_value=found):
            reads = visit_refresh.reads_of("machine", "example-host")

        self.assertEqual(set(reads.kinds), {*fed_kinds("tailscale"), *fed_kinds("ssh")})
        # What the shell connection reads is known to be behind the page, and
        # is still never asked for by opening it.
        for kind in fed_kinds("ssh"):
            self.assertFalse(askable(kind))


@MARKERS
class RefusingHostTests(TestCase):
    """A read somebody asked for does not retry an SSH login a host is refusing."""

    def setUp(self):
        for ref, reachable in (("example-shell", True), ("example-refusing", False)):
            ProviderConnection.objects.create(
                controller_id="example-controller",
                connection_ref=ref,
                provider="ssh",
                probed=True,
                reachable=reachable,
                observed_at=timezone.now() - timedelta(minutes=10),
            )
        store(FAST)
        store("tailscale.device")

    def test_the_sweeps_own_clock_still_asks_a_failing_connection_again(self):
        from ..cadence import sweep_due

        with override_settings(SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=0, SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=0):
            verdict = sweep_due("example-controller")

        self.assertTrue(verdict["due"])
        self.assertEqual(verdict["carry"], ["example-shell"])

    def test_a_read_asked_for_by_an_open_page_leaves_the_refusing_host_alone(self):
        from ..cadence import sweep_due

        ReadRequest.objects.create(kind=FAST)
        verdict = sweep_due("example-controller")

        self.assertTrue(verdict["due"])
        self.assertEqual(verdict["only_kinds"], [FAST])
        self.assertEqual(verdict["carry"], ["example-refusing", "example-shell"])

    def test_asking_for_the_refusing_connection_itself_still_probes_it(self):
        from ..cadence import sweep_due

        ReadRequest.objects.create(connection_ref="example-refusing")
        verdict = sweep_due("example-controller")

        self.assertNotIn("example-refusing", verdict["carry"])
        self.assertIn("example-shell", verdict["carry"])


@MARKERS
class VisitRefreshViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="someone", password="not-used-here")
        self.url = reverse("control_plane:visit")
        store(FAST, age=OLD)

    def post(self, client=None, **body):
        return (client or self.client).post(self.url, {"subject": "example", "name": "known", **body})

    def test_it_needs_a_session(self):
        with page(FAST):
            response = self.post()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(asked(), set())

    def test_a_post_without_the_csrf_token_is_refused(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        with page(FAST):
            response = self.post(client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(asked(), set())

    def test_a_post_asks_and_hands_back_what_to_watch_for(self):
        self.client.force_login(self.user)

        with page(FAST):
            found = self.post().json()

        self.assertEqual(found["requested"], [FAST])
        self.assertTrue(found["live"])
        self.assertEqual(self.client.get(found["status"]).json()["state"], found["state"])

    def test_opening_or_prefetching_the_address_reads_nothing(self):
        self.client.force_login(self.user)

        with page(FAST):
            for query in ({}, {"subject": "example", "name": "known"}, {"watch": "forged"}, {"watch": "x" * 5000}):
                with self.subTest(query=sorted(query)):
                    self.assertEqual(self.client.get(self.url, query).status_code, 405)
                    self.assertEqual(
                        self.client.get(reverse("control_plane:read_status"), query).status_code, 404
                    )

        self.assertEqual(asked(), set())

    def test_the_request_cannot_name_what_to_read(self):
        """A kind or a connection in the request is not an input at all."""

        self.client.force_login(self.user)
        store("tailscale.device", age=OLD)

        with page(FAST):
            self.post(kind="tailscale.device", connection_ref="x", watch="y")

        self.assertEqual(asked(), {FAST})

    def test_an_unknown_subject_or_page_is_not_found(self):
        self.client.force_login(self.user)

        with page(FAST):
            for body in (
                {"subject": "nonsense"},
                {"name": "unknown"},
                {"name": ""},
                {"name": "x" * 300},
            ):
                with self.subTest(body=body):
                    self.assertEqual(self.post(**body).status_code, 404)
        self.assertEqual(asked(), set())

    def test_the_service_page_carries_the_form(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("control_plane:service", args=["example.test"]))

        self.assertContains(response, "data-ask-auto")
        self.assertContains(response, 'name="subject" value="service"')
        self.assertContains(response, 'name="name" value="example.test"')
