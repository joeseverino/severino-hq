"""Open PR has Renovate open the pull request of an app's newer release: it
ticks the box Renovate holds the branch under, and asks a run only once the
tick lands, so the run reads it; with no box, it asks the run at once."""

import re

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import OperationRequest
from hq.domains.projects.models import Project

from ..capabilities import run_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..dependency_updates import asked_at
from ..security import Capability, Principal
from .test_app_updates import arranged
from .test_github_estate import store

OPEN = "app_update.open"
CONTROLLER = "controller-example"
PERSON = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
HELD = {"branch": "renovate/app", "marker": "unpend", "apps": ["app"]}


def tag_of(page, marked):
    """The opening tag of the element ``page`` marks with ``marked``, "" where none is."""

    found = re.search(rf"<[a-z]+[^>]*{re.escape(marked)}[^>]*>", page.content.decode())
    return found[0] if found else ""


class OpenUpdateTests(TestCase):
    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        Project.objects.create(
            name="Apps", slug="apps", repository_url="https://github.com/example/alpha", dependency_updates=True
        )
        arranged()

    def press(self, key="example-open-1"):
        payload = {"repository": "example/alpha", "app": "app"}
        return run_capability(OPEN, payload, principal=PERSON, idempotency_key=key).answer

    def test_a_held_branch_is_ticked_and_the_run_asked_once_the_tick_lands(self):
        store(compose=["compose/app/compose.yaml"], held_branches=[HELD])

        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        tick = OperationRequest.objects.get(action=OperationRequest.Action.RECONCILE)
        self.assertEqual(
            (tick.resource.kind, tick.resource.spec),
            ("github.dashboard_box", {"repository": "example/alpha", "branch": "renovate/app", "marker": "unpend"}),
        )
        self.assertIsNone(asked_at(), "no run is asked before the box is ticked")
        claimed = claim_next_operation(CONTROLLER, capabilities=(("github.dashboard_box", "reconcile"),))
        report_operation(
            claimed["operation"]["id"],
            ControllerReport(success=True, observed_generation=tick.input["generation"]),
            controller_id=CONTROLLER,
        )
        self.assertIsNotNone(asked_at())

    def test_a_release_renovate_holds_nothing_of_asks_the_run_at_once(self):
        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        self.assertIsNotNone(asked_at())
        self.assertFalse(OperationRequest.objects.exists())

    def container_page(self):
        return self.client.get(reverse("control_plane:detail", args=["example-box-app"]))

    def test_a_press_under_way_is_followed_on_its_card_and_a_second_press_asks_nothing_more(self):
        from hq.platform.core.testing import declare, operator

        operator(self.client)
        declare("example-box-app", "portainer.container", {"host": "example-box", "name": "app"})
        store(compose=["compose/app/compose.yaml"], held_branches=[HELD])
        before = self.container_page()
        press = before.context["container"]["act"]
        self.assertNotIn("data-fragment-poll", tag_of(before, 'id="upgrade"'))

        answered = self.client.post(press.url, {"__retry_key": "example-open-1"}, follow=True)

        # The card says where it stands, so the press adds no line of its own.
        self.assertEqual(list(answered.context["messages"]), [])
        page = self.container_page()
        container = page.context["container"]
        self.assertIsNone(container["act"])
        self.assertTrue(container["state"].standing.live)
        # Its card is asked again until it ends.
        self.assertIn("data-fragment-poll", tag_of(page, 'id="upgrade"'))
        again = self.press(key="example-open-2")
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["standing"]["live"])
        tick = OperationRequest.objects.get(action=OperationRequest.Action.RECONCILE)
        self.assertEqual((again["queued"], again["operation"]["id"]), (False, str(tick.pk)))
