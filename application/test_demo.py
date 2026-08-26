"""What a demo may change, and what it must not."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog

from .demo import amount, day, demo_scope, label, showing_demo


class SubstitutionTests(TestCase):
    def test_nothing_is_substituted_until_somebody_asks(self):
        """The call is unconditional at every call site, so the default has to
        be the real value -- a domain that forgot to check must show the truth,
        never a stand-in."""

        self.assertFalse(showing_demo())
        self.assertEqual(amount(Decimal("1234.56"), key="a"), Decimal("1234.56"))
        self.assertEqual(label("Real Name", key="a"), "Real Name")
        self.assertEqual(day(date(2026, 5, 2), key="a"), date(2026, 5, 2))

    def test_the_same_record_shows_the_same_stand_in_every_time(self):
        """A balance that moves on every refresh reads as a broken page rather
        than as a number."""

        with demo_scope(True):
            first = amount(Decimal("110068.00"), key="acct-1")
            second = amount(Decimal("110068.00"), key="acct-1")
            other = amount(Decimal("110068.00"), key="acct-2")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_a_stand_in_keeps_the_magnitude_and_the_sign(self):
        """Six figures replaced by three reads as an empty account, and a page
        laid out for one is misread as broken. What is owed goes on reading as
        owed."""

        with demo_scope(True):
            big = amount(Decimal("110068.00"), key="net-worth")
            small = amount(Decimal("12.34"), key="coffee")
            owed = amount(Decimal("-817.00"), key="card")

        self.assertEqual(len(str(int(big))), 6)
        self.assertEqual(len(str(int(small))), 2)
        self.assertLess(owed, 0)
        self.assertEqual(len(str(int(abs(owed)))), 3)

    def test_magnitude_is_the_only_thing_a_stand_in_carries_over(self):
        """The deliberate leak, pinned so it stays deliberate.

        Everything but the number of digits comes from the key, so two amounts
        of the same size under one key are the same stand-in and nothing finer
        than "six figures" survives. Keeping the size is the trade: a page laid
        out for six figures is misread as broken when handed three.
        """

        with demo_scope(True):
            self.assertEqual(
                amount(Decimal("110068.00"), key="k"),
                amount(Decimal("999999.00"), key="k"),
            )
            self.assertNotEqual(
                amount(Decimal("110068.00"), key="k"),
                amount(Decimal("42.00"), key="k"),
            )

    def test_a_label_is_replaced_rather_than_masked(self):
        """A partially masked name is still a name, and reading half of one is
        how somebody works out the other half."""

        with demo_scope(True):
            stood_in = label("Joseph Severino", key="person-1")
            from_something_else = label("anything else", key="person-1")

        self.assertNotIn("Joseph", stood_in)
        self.assertNotIn("Severino", stood_in)
        # Derived from the key alone, so nothing of the original survives.
        self.assertEqual(stood_in, from_something_else)

    def test_a_date_is_shifted_so_an_ordering_survives(self):
        """A renewal that falls before another still does, so a page that sorts
        by date goes on making sense."""

        earlier, later = date(2026, 1, 1), date(2026, 6, 1)
        with demo_scope(True):
            shifted_earlier = day(earlier, key="one")
            shifted_later = day(later, key="one")

        self.assertNotEqual(shifted_earlier, earlier)
        self.assertLess(shifted_earlier, shifted_later)

    def test_the_scope_is_left_exactly_where_it_was_found(self):
        with demo_scope(True):
            self.assertTrue(showing_demo())
        self.assertFalse(showing_demo())


class ToggleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)

    def test_it_is_off_until_it_is_turned_on_and_survives_the_page(self):
        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.showing_demo)

        self.client.post(reverse("demo_mode"))

        self.assertTrue(self.client.get(reverse("dashboard")).wsgi_request.showing_demo)

    def test_turning_it_off_again_restores_real_values(self):
        self.client.post(reverse("demo_mode"))
        self.client.post(reverse("demo_mode"))

        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.showing_demo)

    def test_a_get_cannot_flip_it(self):
        """A thing that flips by following a link can be flipped by an image
        tag on somebody else's page."""

        self.client.get(reverse("demo_mode"))

        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.showing_demo)

    def test_both_directions_are_recorded(self):
        """An operator who forgets the mode is on can screenshot fiction and
        file it as fact, so the trail says when it was on."""

        self.client.post(reverse("demo_mode"))
        self.client.post(reverse("demo_mode"))

        said = [
            event.message
            for event in AuditLog.objects.filter(object_type="Demo mode").order_by("id")
        ]
        self.assertEqual(said, ["Demo mode on", "Demo mode off"])

    def test_it_returns_to_the_page_it_was_flipped_from(self):
        response = self.client.post(
            reverse("demo_mode"), {"next": reverse("action_items")}
        )

        self.assertRedirects(
            response, reverse("action_items"), fetch_redirect_response=False
        )

    def test_a_destination_off_this_host_is_refused(self):
        response = self.client.post(
            reverse("demo_mode"), {"next": "https://example.test/elsewhere"}
        )

        self.assertRedirects(
            response, reverse("dashboard"), fetch_redirect_response=False
        )

    def test_the_header_carries_a_mark_while_it_is_on(self):
        """The mode outlives whichever page is open, so the header says so.

        Asserted on the mark rather than on its text: on a narrow screen it
        becomes a dot and the word stays only in the accessibility tree, so the
        element is the fact and the rendering is not.
        """

        self.assertNotContains(self.client.get(reverse("dashboard")), 'class="demo-flag"')

        self.client.post(reverse("demo_mode"))

        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, 'class="demo-flag"')
        self.assertContains(page, "Demo")

    def test_the_switch_carries_the_state_rather_than_the_label(self):
        """A menu entry that renames itself makes you read the button to work
        out what is currently true."""

        off = self.client.get(reverse("dashboard"))
        self.assertContains(off, 'role="switch" aria-checked="false"')

        self.client.post(reverse("demo_mode"))

        on = self.client.get(reverse("dashboard"))
        self.assertContains(on, 'role="switch" aria-checked="true"')

    def test_signing_out_does_not_carry_the_mode_to_the_next_operator(self):
        self.client.post(reverse("demo_mode"))
        self.client.post(reverse("logout"))

        other = get_user_model().objects.create_user("other", password="x" * 20)
        self.client.force_login(other)

        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.showing_demo)
