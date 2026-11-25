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
from hq.platform.application import roles
from hq.platform.application.markup_testing import elements
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.consent import consented
from hq.platform.core.test_runner import no_default_role
from hq.platform.core.testing import declare

from ..controller import ControllerReport, claim_next_operation, report_operation
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
        super().setUp()
        from hq.platform.core.testing import managing_everything

        managing_everything()

    def test_requesting_removal_queues_work_and_keeps_the_row(self):
        """The declaration outlives the request, because the record still exists.

        Dropping it here would leave the rewrite live in AdGuard with nothing in
        HQ describing it, and no way to ask for its removal a second time.
        """
        resource = a_rewrite()

        result = request_removal(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key=resource.key,
        )

        self.assertTrue(result["queued"])
        self.assertEqual(result["operation"]["action"], "delete")
        self.assertTrue(ManagedResource.objects.filter(key="app-dns").exists())

    def test_a_disabled_declaration_can_still_be_removed(self):
        """Disabling is what something looks like just before it is deleted.

        The enabled check exists to stop HQ converging a paused declaration.
        Removal is the opposite of converging it, so the check does not apply.
        """
        resource = a_rewrite(enabled=False)

        result = request_removal(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key=resource.key,
        )

        self.assertTrue(result["queued"])

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

    def test_asking_twice_queues_one_operation(self):
        resource = a_rewrite()
        for _ in range(2):
            request_removal(
                OperationCommand(),
                consented=consented(cli_principal()),
                current_key=resource.key,
            )

        self.assertEqual(OperationRequest.objects.filter(action="delete").count(), 1)


class RemovalReportTests(TestCase):
    """HQ forgets the declaration only once the provider is confirmed clear."""

    def setUp(self):
        super().setUp()
        from hq.platform.core.testing import managing_everything

        managing_everything()

    def _claimed_removal(self) -> tuple[ManagedResource, dict]:
        resource = a_rewrite()
        request_removal(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key=resource.key,
        )
        claim = claim_next_operation("test-controller", capabilities=(("adguard.rewrite", "delete"),))
        return resource, claim["operation"]

    def _report(self, operation, *, success: bool) -> dict:
        return report_operation(
            operation["id"],
            ControllerReport(
                success=success,
                observed_generation=1,
                status={"domain": "app.example.com", "removed": success},
                conditions=[
                    {
                        "type": "Ready" if success else "Degraded",
                        "status": True,
                        "reason": "Removed" if success else "ProviderError",
                        "message": "",
                    }
                ],
                message="",
            ),
            controller_id="test-controller",
        )

    def test_a_confirmed_removal_forgets_the_declaration(self):
        _, operation = self._claimed_removal()

        result = self._report(operation, success=True)

        self.assertTrue(result["removed"])
        # The caller still gets the resource it asked about, serialized before
        # the row went, rather than a different answer shape for this one verb.
        self.assertEqual(result["resource"]["key"], "app-dns")
        self.assertFalse(ManagedResource.objects.filter(key="app-dns").exists())
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_failed_removal_keeps_everything(self):
        """The record is still at the provider, so the declaration is still true."""
        _, operation = self._claimed_removal()

        self._report(operation, success=False)

        self.assertTrue(ManagedResource.objects.filter(key="app-dns").exists())
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.FAILED)

    def test_a_successful_reconcile_never_forgets_anything(self):
        """Only the delete action removes. Guarded because the cost is total."""
        resource = a_rewrite()
        resource.generation = 1
        resource.save(update_fields=["generation"])
        from ..resource_operations import request_reconcile

        request_reconcile(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key=resource.key,
        )
        claim = claim_next_operation("test-controller", capabilities=(("adguard.rewrite", "reconcile"),))

        result = self._report(claim["operation"], success=True)

        self.assertNotIn("removed", result)
        self.assertTrue(ManagedResource.objects.filter(key="app-dns").exists())


class RemovalWebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="test-only-password")
        self.client.force_login(self.user)
        a_rewrite()

    def test_through_an_observing_connection_it_forgets_and_deletes_nothing(self):
        from hq.platform.core.testing import connection

        connection("adguard", manages=False)
        response = self.client.post(
            reverse("control_plane:remove", kwargs={"key": "app-dns"}),
            form_keyed({"reason": "Retired."}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OperationRequest.objects.exists())
        self.assertFalse(ManagedResource.objects.filter(key="app-dns").exists())

    def test_the_page_asks_before_doing_anything(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        response = self.client.get(reverse("control_plane:remove", kwargs={"key": "app-dns"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "deletes the live record")
        self.assertFalse(OperationRequest.objects.exists())

    def test_the_reason_is_asked_only_beside_the_press_that_sends_it(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        url = reverse("control_plane:remove", kwargs={"key": "app-dns"})
        for role, offered in ((roles.Role.OPERATOR, True), (roles.Role.VIEWER, False)):
            with self.subTest(role), no_default_role():
                user = get_user_model().objects.create_user(username=f"example-{role}")
                roles.assign(user, role)
                self.client.force_login(user)
                page = self.client.get(url)

                reasons = [each for each in elements(page, "input") if each.attrs.get("name") == "reason"]
                presses = [each for each in elements(page, "button") if each.attrs.get("formaction") == url]
                self.assertEqual((bool(reasons), bool(presses)), (offered, offered))

    def test_confirming_queues_removal_without_dropping_the_row(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        response = self.client.post(
            reverse("control_plane:remove", kwargs={"key": "app-dns"}),
            form_keyed({"reason": "Retired."}),
        )

        self.assertRedirects(response, reverse("control_plane:detail", kwargs={"key": "app-dns"}))
        operation = OperationRequest.objects.get()
        self.assertEqual(operation.action, "delete")
        self.assertEqual(operation.reason, "Retired.")
        self.assertTrue(ManagedResource.objects.filter(key="app-dns").exists())
