"""Open PR has Renovate open the pull request of an app's newer release: it
asks a run of the app's one repository, naming the branch Renovate holds it
under as the run's checked branch, and the run's outcome says what became
of it."""

import re

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest
from hq.domains.projects.models import Project

from ..capabilities import run_capability
from ..dependency_updates import KIND, plan
from ..inventory_testing import store as stored
from ..read_requests import forced_reads
from ..security import Capability, Principal
from .test_app_updates import arranged
from .test_github_estate import store

OPEN = "app_update.open"
PERSON = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
FILES = ["compose/app/compose.yaml"]
HELD = {"repository": "example/alpha", "branch": "renovate/app", "result": "pending", "files": FILES}


def ran(*branches, outcome="ok"):
    """The latest run of example/alpha, as its report left ``branches``."""

    at = timezone.now().isoformat()
    record = {"owner": "example", "repositories": ["example/alpha"], "outcome": outcome, "started_at": at}
    stored(KIND, {**record, "branches": [{"pull_request": None, **each} for each in branches]})


def tag_of(page, marked):
    """The opening tag of the element ``page`` marks with ``marked``, "" where none is."""

    found = re.search(rf"<[a-z]+[^>]*{re.escape(marked)}[^>]*>", page.content.decode())
    return found[0] if found else ""


class OpenUpdateTests(TestCase):
    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        for name in ("alpha", "beta"):
            Project.objects.create(
                name=name, slug=name, repository_url=f"https://github.com/example/{name}", dependency_updates=True
            )
        arranged()
        store(compose=FILES)

    def press(self, key="example-open-1"):
        payload = {"repository": "example/alpha", "app": "app"}
        return run_capability(OPEN, payload, principal=PERSON, idempotency_key=key).answer

    def test_a_held_branch_is_asked_as_the_checked_branch_of_its_repository_alone(self):
        ran(HELD)

        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        found = plan()
        self.assertEqual(
            (found["owners"], found["due"]),
            ([{"owner": "example", "repositories": ["example/alpha"], "branches": ["renovate/app"]}], True),
        )
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_release_renovate_holds_nothing_of_asks_a_run_of_its_repository(self):
        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(plan()["owners"], [{"owner": "example", "repositories": ["example/alpha"], "branches": []}])

    def container_page(self):
        return self.client.get(reverse("control_plane:detail", args=["example-box-app"]))

    def test_a_press_under_way_is_said_on_its_card_and_a_second_press_asks_nothing_more(self):
        from hq.platform.core.testing import declare, operator

        operator(self.client)
        declare("example-box-app", "portainer.container", {"host": "example-box", "name": "app"})
        ran(HELD)
        before = self.container_page()
        press = before.context["container"]["act"]

        answered = self.client.post(press.url, {"__retry_key": "example-open-1"}, follow=True)

        # The card says where it stands, so the press adds no line of its own.
        self.assertEqual(list(answered.context["messages"]), [])
        page = self.container_page()
        container = page.context["container"]
        self.assertIsNone(container["act"])
        self.assertTrue(container["state"].pull.live)
        asked = forced_reads()
        again = self.press(key="example-open-2")
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["standing"]["live"])
        self.assertEqual(forced_reads(), asked)

    def test_a_pulse_of_its_card_draws_the_card_alone_or_nothing_while_nothing_it_reads_was_written(self):
        from hq.platform.core.testing import declare, operator

        operator(self.client)
        declare("example-box-app", "portainer.container", {"host": "example-box", "name": "app"})
        address = reverse("control_plane:detail", args=["example-box-app"])

        part = self.client.get(address, headers={"X-Fragment": "upgrade"})

        self.assertEqual([tag_of(part, 'id="upgrade"') != "", 'id="supply-chain"' in part.text], [True, False])
        unchanged = self.client.get(address, headers={"X-Fragment": "upgrade", "If-None-Match": part["ETag"]})
        self.assertEqual(unchanged.status_code, 304)
