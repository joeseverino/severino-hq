from django.test import TestCase

from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator

from ..linked_accounts import GITHUB, linked_login, record_claimed_accounts


class RecordTests(TestCase):
    def setUp(self):
        self.user = operator()

    def test_a_sign_in_without_the_claim_forgets_it(self):
        record_claimed_accounts(self.user, {"github": "example-user"})
        record_claimed_accounts(self.user, {})

        self.assertEqual(linked_login(self.user, GITHUB), "")
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DELETED).exists())
