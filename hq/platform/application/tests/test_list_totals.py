"""A list read says how many matched and whether its page holds them all,
counted rather than guessed from how full the page is."""

from unittest import mock

from django.test import TestCase

from hq.domains.contacts.inbox import Inbox

from ..resources import list_resource
from ..security import cli_principal
from .test_hq_context import SITE, estate, own


def listed(name, query=None):
    first, second = own("192.0.2.44")
    with first, second:
        return list_resource(name, query or {}, principal=cli_principal())


@SITE
class ListTotalTests(TestCase):
    def test_a_list_read_whole_counts_every_item(self):
        estate()
        for name in ("hq", "services"):
            with self.subTest(name=name):
                found = listed(name)

                self.assertEqual(found["total"], found["count"])
                self.assertFalse(found["truncated"])

    def test_a_cut_off_page_counts_what_it_was_cut_from(self):
        estate(containers=2)
        rows = tuple({"id": index, "name": f"Example {index}", "status": "new"} for index in range(3))
        with mock.patch("hq.domains.contacts.inbox.current", return_value=Inbox("ok", rows=rows)):
            pages = {name: listed(name, {"limit": 1}) for name in ("containers", "contact.submissions")}
        whole = {"containers": 3, "contact.submissions": 3}

        for name, found in pages.items():
            with self.subTest(name=name):
                self.assertEqual(found["count"], 1)
                self.assertEqual(found["total"], whole[name])
                self.assertTrue(found["truncated"])
                self.assertEqual(set(found["next"]), {"cursor"})
