"""A page reads the adopted playbook catalog only where it draws something of it."""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from hq.domains.control_plane.models import AdoptedCatalog
from hq.platform.core.testing import SignedInTestCase

# Pages that name commands (a page action, a tool, an audit row's words) and
# draw no playbook, so the catalog has no job on them.
PAGES = (
    "docs_index:list",
    "calendar:month",
    "expenses:list",
    "receipts:list",
    "assets:list",
    "projects:list",
    "watching",
    "control_plane:topology",
    "control_plane:tools",
    "core:audit_list",
)


class CatalogReadTests(SignedInTestCase):
    def test_a_page_that_draws_no_playbook_reads_no_catalog(self):
        for name in PAGES:
            with self.subTest(name), CaptureQueriesContext(connection) as sent:
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                read = [query["sql"] for query in sent if AdoptedCatalog._meta.db_table in query["sql"]]
                self.assertEqual(read, [])
