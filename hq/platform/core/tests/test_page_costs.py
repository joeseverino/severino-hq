"""`manage.py page_costs` walks every listed page as an operator, under production's settings, and
says when it could not."""

import io
import json
from unittest import mock

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import TestCase, modify_settings

from hq.platform.core.example_estate.testing import as_production
from hq.platform.core.testing import operator

MISSING = "/example-missing/"


def walked(*pages: str) -> dict:
    out = io.StringIO()
    call_command("page_costs", *pages, stdout=out)
    return json.loads(out.getvalue().strip().splitlines()[-1])


# A session whose ID token has aged out is sent to the identity provider for a new one, as
# production's is.
@modify_settings(MIDDLEWARE={"append": "hq.platform.core.oidc.HQSessionRefresh"})
class PageCostsTests(TestCase):
    def test_every_listed_page_answers_the_operator_under_production_settings(self):
        operator()
        with as_production():
            listed = walked()
            missing = walked(MISSING)

        self.assertNotIn("unread", listed)
        self.assertTrue(listed["revision"])
        self.assertIn("/", listed["pages"])
        for page, cost in listed["pages"].items():
            with self.subTest(page=page):
                self.assertEqual(cost["status"], 200)
                self.assertGreater(cost["queries"], 0)
                self.assertGreater(cost["html_bytes"], 0)
        self.assertEqual(missing["pages"][MISSING]["status"], 404)
        self.assertFalse(Session.objects.exists(), "the walk's session outlived it")

    def test_with_no_operator_nothing_is_read(self):
        said = walked()

        self.assertEqual(said["pages"], {})
        self.assertEqual(said["unread"], "no active operator to sign in as")

    def test_a_walk_its_session_does_not_sign_in_is_unread(self):
        operator()
        with as_production(), mock.patch("hq.platform.core.page_costs.new_session", return_value=SessionStore()):
            said = walked("/")

        self.assertEqual(said["pages"], {})
        self.assertTrue(said["unread"])
