"""A finding's remedy is its command's: what it does and where it is pressed."""

from django.test import TestCase

from ..capabilities import command_spec
from ..finding_model import FindingRemedy
from ..pages import command_reach
from ..resource_capabilities import UPDATE_CAPABILITY


class RemedyTests(TestCase):
    def test_its_effect_is_the_command_s_and_it_is_reached_where_the_command_is(self):
        remedy = FindingRemedy(capability=UPDATE_CAPABILITY, target="example-key")
        url, answers_get = command_reach(UPDATE_CAPABILITY, "example-key")

        self.assertEqual(remedy.effect, command_spec(UPDATE_CAPABILITY).effect)
        self.assertEqual(remedy.reach, (url, "GET" if answers_get else "POST"))

    def test_a_route_that_runs_it_on_something_else_is_kept(self):
        remedy = FindingRemedy(
            capability="infrastructure.resource.create", target="web", url="/example/adopt/", method="POST"
        )

        self.assertEqual(remedy.reach, ("/example/adopt/", "POST"))

    def test_its_effect_is_not_its_own_to_say(self):
        with self.assertRaises(TypeError):
            FindingRemedy(capability=UPDATE_CAPABILITY, target="example-key", effect="read")  # type: ignore[call-arg]
