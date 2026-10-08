from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from hq.platform.core.models import AuditLog, LinkedAccount

from ..linked_accounts import GITHUB, claimed_github_login, linked_login, record_claimed_accounts


class ClaimTests(SimpleTestCase):
    def test_a_login_github_could_issue_is_taken(self):
        self.assertEqual(claimed_github_login({"github": " example-user "}), "example-user")

    def test_anything_else_is_not(self):
        for value in (None, 7, ["example"], "", "-leading", "trailing-", "two--hyphens", "a" * 40, "has space", "x/y"):
            with self.subTest(value=value):
                self.assertEqual(claimed_github_login({"github": value}), "")


class RecordTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator")

    def test_a_sign_in_that_claims_an_account_links_it_once(self):
        record_claimed_accounts(self.user, {"github": "example-user"})
        record_claimed_accounts(self.user, {"github": "example-user"})

        self.assertEqual(linked_login(self.user, GITHUB), "example-user")
        # Unchanged on the second sign-in, so audited once.
        self.assertEqual(
            AuditLog.objects.filter(message="Your sign-in names the github account example-user").count(), 1
        )

    def test_a_changed_claim_replaces_the_login(self):
        record_claimed_accounts(self.user, {"github": "old-name"})
        record_claimed_accounts(self.user, {"github": "new-name"})

        self.assertEqual(linked_login(self.user, GITHUB), "new-name")
        self.assertEqual(LinkedAccount.objects.filter(user=self.user).count(), 1)

    def test_a_sign_in_without_the_claim_forgets_it(self):
        record_claimed_accounts(self.user, {"github": "example-user"})
        record_claimed_accounts(self.user, {})

        self.assertEqual(linked_login(self.user, GITHUB), "")
        self.assertTrue(AuditLog.objects.filter(message__contains="no longer names").exists())

    def test_an_unsaved_user_records_nothing(self):
        record_claimed_accounts(get_user_model()(username="ghost"), {"github": "example-user"})

        self.assertFalse(LinkedAccount.objects.exists())
