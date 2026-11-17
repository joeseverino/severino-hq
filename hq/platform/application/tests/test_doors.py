"""Who did something reads in plain words, HQ's own machinery included."""

from django.test import SimpleTestCase

from hq.platform.application.doors import Attribution, door_words
from hq.platform.application.security import CONTROLLER_INTERFACE, JOB_INTERFACE, WEB_INTERFACE

# Written out, so the module imports at a ref without the names.
CONTROLLER = "controller"
SCHEDULER = "timer"


class MachineryAttributionTests(SimpleTestCase):
    def test_the_controller_is_named_once(self):
        said = Attribution(CONTROLLER, CONTROLLER_INTERFACE).words

        self.assertEqual(said.lower().count("controller"), 1, said)

    def test_a_scheduled_job_says_it_was_the_schedule_and_not_its_internal_name(self):
        said = Attribution(SCHEDULER, JOB_INTERFACE).words

        self.assertNotIn(SCHEDULER, said)
        self.assertNotIn(door_words(JOB_INTERFACE), said)
        self.assertIn("schedule", said)

    def test_a_person_who_asked_for_a_job_keeps_the_door(self):
        said = Attribution("example", JOB_INTERFACE).words

        self.assertIn("example", said)
        self.assertIn(door_words(JOB_INTERFACE), said)

    def test_a_person_named_as_the_machinery_is_named_as_themselves(self):
        said = Attribution(CONTROLLER, WEB_INTERFACE).words

        self.assertIn(door_words(WEB_INTERFACE), said)

    def test_who_approved_it_is_still_said(self):
        said = Attribution(CONTROLLER, CONTROLLER_INTERFACE, approved_by="example").words

        self.assertIn("example", said)
