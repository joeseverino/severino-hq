"""What an operator keeps at the top, and in what order.

A pin is a preference about a person, never about the thing pinned: starring a
hostname must not reach a spec, bump a generation or queue a reconcile. The
order is the substance of it -- alphabetical among favorites is an ordering
nobody chose, which is the whole reason the list needed one.
"""

from __future__ import annotations

from django.test import TestCase

class ServiceFavoriteTests(TestCase):

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="an-operator", password="not-used-here"
        )

    def order(self):
        from application.pins import SERVICE, ordered

        return list(ordered(self.user, SERVICE))

    def pin(self, *names):
        from application.pins import SERVICE, toggle

        for name in names:
            toggle(self.user, SERVICE, name)

    def test_a_new_favorite_lands_at_the_end(self):
        """Anywhere else silently rearranges what was already arranged."""

        self.pin("one.example.test", "two.example.test", "three.example.test")

        self.assertEqual(
            self.order(),
            ["one.example.test", "two.example.test", "three.example.test"],
        )

    def test_moving_one_up_swaps_it_with_its_neighbour(self):
        from application.pins import SERVICE, move

        self.pin("one.example.test", "two.example.test", "three.example.test")
        move(self.user, SERVICE, "three.example.test", -1)

        self.assertEqual(
            self.order(),
            ["one.example.test", "three.example.test", "two.example.test"],
        )

    def test_moving_past_the_end_changes_nothing(self):
        from application.pins import SERVICE, move

        self.pin("one.example.test", "two.example.test")
        move(self.user, SERVICE, "one.example.test", -1)

        self.assertEqual(self.order(), ["one.example.test", "two.example.test"])

    def test_unstarring_leaves_the_rest_in_order(self):
        from application.pins import SERVICE, toggle

        self.pin("one.example.test", "two.example.test", "three.example.test")
        toggle(self.user, SERVICE, "two.example.test")

        self.assertEqual(
            self.order(), ["one.example.test", "three.example.test"]
        )

    def test_reordering_cannot_pin_something_new(self):
        """An order says what comes first, not what belongs in the list."""

        from application.pins import SERVICE, reorder

        self.pin("one.example.test")
        reorder(self.user, SERVICE, ["a-stranger.example.test", "one.example.test"])

        self.assertEqual(self.order(), ["one.example.test"])

    def test_an_anonymous_visitor_has_no_favorites(self):
        from django.contrib.auth.models import AnonymousUser

        from application.pins import SERVICE, ordered

        self.assertEqual(ordered(AnonymousUser(), SERVICE), ())
