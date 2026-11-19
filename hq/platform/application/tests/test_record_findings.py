"""A record section's backlog is a finding of its rule: the queue, the findings
every door reads and the reader's gate all come from that one derivation."""

from datetime import date
from unittest import mock

from django.test import TestCase

from hq.domains.calendars.models import Entry
from hq.platform.core.testing import READER

from ..attention import readable_by
from ..dashboard import work_queue
from ..findings import findings
from ..security import Capability, Principal

CONTACTS = Principal("contacts", "test", frozenset({Capability.READ, Capability.MANAGE_CONTACTS}))
CALENDAR = Principal("calendar", "test", frozenset({Capability.READ, Capability.READ_CALENDAR}))
# Two messages nobody has read, as the last read of the contact form's store left them.
UNREAD = mock.patch("hq.domains.contacts.inbox.unread", return_value=(2, "ok"))
WITHOUT_RECEIPT = "1 expense has no receipt"
UNREAD_TITLE = "2 unread messages from the contact form"


def titles(principal: Principal) -> list[str]:
    return [finding["title"] for finding in findings(principal=principal)["findings"]]


def queued(principal: Principal) -> dict[str, dict]:
    return {item["label"]: item for item in readable_by(principal, work_queue())}


class RecordFindingTests(TestCase):
    def test_a_reader_of_a_section_is_told_its_backlog(self):
        with UNREAD:
            self.assertIn(UNREAD_TITLE, titles(CONTACTS))
            self.assertEqual(queued(CONTACTS)[UNREAD_TITLE]["source_id"], "hq.contacts")

    def test_a_reader_who_may_not_read_a_section_is_not_told_its_backlog(self):
        Entry.objects.create(title="Check", starts_on=date(2026, 3, 1), about="asset:gone")
        with UNREAD:
            told = [*titles(READER), *queued(READER)]

        self.assertNotIn(UNREAD_TITLE, told)
        self.assertNotIn("Check links to gone, which HQ does not have", told)
