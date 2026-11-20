"""A projection's offer of a command reaches it as a page's control does: at
the route whose view runs it, posted where that view draws no page; and an
offer that runs no command only opens a page."""

from django.test import RequestFactory, TestCase
from django.urls import reverse

from hq.platform.application import request_context, roles
from hq.platform.application.action_links import command_action
from hq.platform.application.approvals import APPROVE, DECISIONS
from hq.platform.application.contracts import Effect
from hq.platform.application.pages import PageAction
from hq.platform.application.resource_capabilities import REMOVAL_CAPABILITY, UPDATE_CAPABILITY
from hq.platform.core.testing import person

RECONCILE = "infrastructure.resource.reconcile"
KEY = "example-key"


class CommandOfferTests(TestCase):
    def setUp(self):
        self.operator = person(username="operator")
        roles.assign(self.operator, roles.Role.OPERATOR)

    def control(self, name, target):
        request = RequestFactory().get("/")
        request.user = self.operator
        with request_context.bind(request):
            return PageAction.command(name, target)

    def test_an_offer_is_addressed_and_pressed_as_the_page_s_control_for_its_command(self):
        for name in (UPDATE_CAPABILITY, REMOVAL_CAPABILITY, RECONCILE):
            with self.subTest(command=name):
                offer = command_action("offer", name, target=KEY)
                control = self.control(name, KEY)

                self.assertEqual((offer.url, offer.method.lower()), (control.url, control.method))
                self.assertEqual(offer.target, KEY)

    def test_an_offer_at_a_route_of_several_arguments_is_posted_where_its_view_draws_no_page(self):
        approval = "00000000-0000-4000-8000-000000000001"

        offer = command_action(APPROVE, APPROVE, target={"approval_id": approval, "decision": DECISIONS[APPROVE]})

        self.assertEqual(
            (offer.url, offer.method, offer.target),
            (reverse("control_plane:approval_decide", args=[approval, DECISIONS[APPROVE]]), "POST", ""),
        )

    def test_a_link_that_runs_no_command_only_opens_a_page(self):
        from hq.platform.application.action_links import read_link

        link = read_link("subject", "Example", "/example/")

        self.assertEqual((link.effect, link.method, link.capability), (Effect.READ, "GET", ""))
