"""What an operator keeps at the top, and in what order.

A pin is a preference about a person, never about the thing pinned: starring a
hostname must not reach a spec, bump a generation or queue a reconcile. The
order is the substance of it: alphabetical among favorites is an ordering
nobody chose, which is the whole reason the list needed one.
"""

from django.test import TestCase


class ServiceFavoriteTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="an-operator", password="not-used-here")

    def order(self):
        from hq.platform.application.pins import SERVICE, ordered

        return list(ordered(self.user, SERVICE))

    def pin(self, *names):
        from hq.platform.application.pins import SERVICE, toggle

        for name in names:
            toggle(self.user, SERVICE, name)

    def test_a_new_favorite_lands_at_the_end(self):
        """Anywhere else silently rearranges what was already arranged."""

        self.pin("one.example.test", "two.example.test", "three.example.test")

        self.assertEqual(
            self.order(),
            ["one.example.test", "two.example.test", "three.example.test"],
        )

    def test_moving_one_up_swaps_it_with_its_neighbor(self):
        from hq.platform.application.pins import SERVICE, move

        self.pin("one.example.test", "two.example.test", "three.example.test")
        move(self.user, SERVICE, "three.example.test", -1)

        self.assertEqual(
            self.order(),
            ["one.example.test", "three.example.test", "two.example.test"],
        )

    def test_moving_past_the_end_changes_nothing(self):
        from hq.platform.application.pins import SERVICE, move

        self.pin("one.example.test", "two.example.test")
        move(self.user, SERVICE, "one.example.test", -1)

        self.assertEqual(self.order(), ["one.example.test", "two.example.test"])

    def test_unstarring_leaves_the_rest_in_order(self):
        from hq.platform.application.pins import SERVICE, toggle

        self.pin("one.example.test", "two.example.test", "three.example.test")
        toggle(self.user, SERVICE, "two.example.test")

        self.assertEqual(self.order(), ["one.example.test", "three.example.test"])

    def test_reordering_cannot_pin_something_new(self):
        """An order says what comes first, not what belongs in the list."""

        from hq.platform.application.pins import SERVICE, reorder

        self.pin("one.example.test")
        reorder(self.user, SERVICE, ["a-stranger.example.test", "one.example.test"])

        self.assertEqual(self.order(), ["one.example.test"])

    def test_an_anonymous_visitor_has_no_favorites(self):
        from django.contrib.auth.models import AnonymousUser

        from hq.platform.application.pins import SERVICE, ordered

        self.assertEqual(ordered(AnonymousUser(), SERVICE), ())

    def test_membership_is_derived_from_the_ordered_read(self):
        from hq.platform.application.pins import SERVICE, ordered, pinned
        from hq.platform.application.projection import projection_scope
        from hq.platform.core.models import Pin

        Pin.objects.create(
            user=self.user,
            target_kind=SERVICE,
            target_key="Mixed.Example.Test",
            position=0,
        )

        with projection_scope():
            ordered(self.user, SERVICE)
            with self.assertNumQueries(0):
                result = pinned(self.user, SERVICE)

        self.assertEqual(result, frozenset({"mixed.example.test"}))


class PinCapabilityTests(TestCase):
    """A pin is the person's own: kept through the chokepoint, audited, and refused to any token."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="an-operator", password="not-used-here")
        self.client.force_login(self.user)

    def test_each_page_pins_through_the_chokepoint_and_is_audited(self):
        from django.urls import reverse

        from hq.platform.application.pins import CHOOSE, DOMAIN, MOVE, PIN, SERVICE, ordered
        from hq.platform.core.models import AuditLog

        self.client.post(reverse("zones:pin", args=["example.com"]))
        self.client.post(reverse("control_plane:service_pin", args=["one.example.test"]))
        self.client.post(reverse("control_plane:service_pin", args=["two.example.test"]))
        self.client.post(reverse("control_plane:service_move", args=["two.example.test"]), {"direction": "up"})
        self.client.post(reverse("dashboard_links"), {"href": []})

        self.assertEqual(list(ordered(self.user, DOMAIN)), ["example.com"])
        self.assertEqual(list(ordered(self.user, SERVICE)), ["two.example.test", "one.example.test"])
        for operation in (PIN, MOVE, CHOOSE):
            with self.subTest(operation=operation):
                self.assertTrue(
                    AuditLog.objects.filter(
                        user=self.user, metadata__operation=operation, metadata__actor="an-operator"
                    ).exists()
                )

    def test_a_token_is_refused_whatever_it_was_granted(self):
        from hq.platform.application.capabilities import execute_capability
        from hq.platform.application.pins import PIN, SERVICE
        from hq.platform.application.security import Capability, Principal, cli_principal, internal_principal
        from hq.platform.core.models import AuditLog, Pin

        granted = frozenset({Capability.PIN_FAVORITES})
        for principal in (
            Principal("an-operator", "api", granted, granted=granted),
            Principal("an-operator", "mcp", granted, granted=granted),
            cli_principal(),
            internal_principal("example-purpose"),
        ):
            with self.subTest(interface=principal.interface):
                result = execute_capability(PIN, {"kind": SERVICE, "key": "one.example.test"}, principal=principal)

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(Pin.objects.exists())
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=PIN).count(), 4)

    def test_the_page_keeps_nothing_when_refused(self):
        from unittest import mock

        from django.urls import reverse

        from hq.platform.application.security import Capability, Principal
        from hq.platform.core.models import Pin

        lacking = Principal("an-operator", "web", frozenset({Capability.READ}))
        with mock.patch("hq.domains.control_plane.zone_views.web_principal", return_value=lacking):
            response = self.client.post(reverse("zones:pin", args=["example.com"]), follow=True)

        self.assertContains(response, Capability.PIN_FAVORITES.value)
        self.assertFalse(Pin.objects.exists())

    def test_an_unknown_kind_or_an_empty_key_is_invalid_input(self):
        from hq.platform.application.capabilities import execute_capability
        from hq.platform.application.pins import MOVE, PIN, SERVICE
        from hq.platform.application.security import OPERATOR_CAPABILITIES, Principal
        from hq.platform.core.models import Pin

        operator = Principal("an-operator", "web", OPERATOR_CAPABILITIES)
        for name, payload in (
            (PIN, {"kind": "example-kind", "key": "one.example.test"}),
            (PIN, {"kind": SERVICE, "key": " "}),
            (MOVE, {"kind": SERVICE}),
        ):
            with self.subTest(payload=payload):
                result = execute_capability(name, payload, principal=operator)

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(Pin.objects.exists())
