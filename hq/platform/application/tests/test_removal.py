"""Removing a record, rather than forgetting HQ's note of it.

The thing a declaration describes lives at a provider. Dropping the row alone
would abandon the rewrite or proxy host with nothing left pointing at it, and
that orphan cannot be found again through HQ, which is the failure this whole
verb exists to prevent.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.consent import consented
from hq.platform.core.testing import connection, declare, managing_everything

from ..record_errors import PolicyError
from ..resource_operations import OperationCommand, request_removal
from ..security import cli_principal

REWRITE = {"domain": "app.example.com", "answer": "10.0.0.10"}


def a_rewrite(**overrides) -> ManagedResource:
    return ManagedResource.objects.create(
        key=overrides.pop("key", "app-dns"),
        kind="adguard.rewrite",
        spec=REWRITE,
        **overrides,
    )


class RemovalRequestTests(TestCase):
    def setUp(self):
        managing_everything()

    def test_removal_is_refused_where_the_controller_cannot_do_it(self):
        """A certificate declares no delete, so nothing may queue one.

        The rule is not about any particular provider: a resource whose kind
        declares no such action must be refused here rather than queued for a
        worker that would find no handler for it.

        A domain needs no delete: removing it ends a responsibility rather than
        destroying anything.
        """

        declare(
            "a-certificate",
            "tls.certificate",
            {"certificate_name": "example", "domains": ["example.com"], "install_on": ["a-target"]},
        )

        with self.assertRaises(PolicyError):
            request_removal(
                OperationCommand(),
                consented=consented(cli_principal()),
                current_key="a-certificate",
            )


class RemovalWebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="test-only-password")
        self.client.force_login(self.user)
        a_rewrite()

    def test_through_an_observing_connection_it_forgets_and_deletes_nothing(self):
        connection("adguard", manages=False)
        response = self.client.post(
            reverse("control_plane:remove", kwargs={"key": "app-dns"}),
            form_keyed({"reason": "Retired."}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OperationRequest.objects.exists())
        self.assertFalse(ManagedResource.objects.filter(key="app-dns").exists())
