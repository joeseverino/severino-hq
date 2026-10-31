"""A record whose settings moved is applied whoever moved them, with nobody queueing the apply.

The controller runs a pass only when HQ rings or its floor comes round, so
`cadence.pass_due` is what has to notice a moved generation; a writer that
forgot to queue its own reconcile would otherwise wait for the floor.
"""

import tempfile
from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.db.models import F
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hq.platform.application import cadence
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.certificates import UploadCertificateCommand, store_certificate
from hq.platform.application.controller import schedule_automatic_operations
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.test_certificates import A_KEY, a_certificate

from ..models import ManagedResource, OperationRequest, ProviderInventory
from .test_control_plane import certificate_spec, declare_targets

PROXY = {
    "domain_names": ["app.example.com"],
    "forward_scheme": "http",
    "forward_host": "192.0.2.10",
    "forward_port": 8000,
}
RECONCILE = OperationRequest.Action.RECONCILE


class _Estate(TestCase):
    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.doorbell = directory / "doorbell"
        self.enterContext(
            override_settings(
                SEVERINO_CONTROLLER_DOORBELL=str(self.doorbell),
                SEVERINO_ACTIVITY_MARKER=str(directory / "activity"),
                SEVERINO_SECRET_STORE_KEY=A_KEY,
            )
        )
        managing_everything()
        # Read a moment ago, so no sweep is due and only the record can make a pass due.
        ProviderInventory.objects.create(kind="adguard.rewrite", records=[], reachable=True, observed_at=timezone.now())

    @staticmethod
    def settle():
        """Every record applied as it stands, and nothing waiting."""

        ManagedResource.objects.update(observed_generation=F("generation"))
        OperationRequest.objects.all().delete()

    @staticmethod
    def unqueue():
        """Drop whatever the writer queued itself: the pass must not depend on it."""

        OperationRequest.objects.all().delete()

    def assert_applied(self, key):
        self.assertTrue(cadence.pass_due())
        schedule_automatic_operations("example-controller")
        resource = ManagedResource.objects.get(key=key)
        operation = OperationRequest.objects.get(resource=resource, action=RECONCILE)
        self.assertEqual(operation.state, OperationRequest.State.QUEUED)
        self.assertEqual(operation.input["generation"], resource.generation)

    def proxy(self, key="example-proxy", **spec):
        return save_managed_resource(
            ManagedResourceCommand(key=key, kind="npm.proxy_host", spec={**PROXY, **spec}),
            principal=cli_principal(),
        )


class EveryWriterIsAppliedTests(_Estate):
    """Each way a generation moves reaches the controller through `pass_due` alone."""

    def test_an_update(self):
        self.proxy()
        self.settle()

        result = execute_capability(
            "infrastructure.resource.update",
            {"key": "example-proxy", "kind": "npm.proxy_host", "spec": {**PROXY, "forward_port": 8080}},
            principal=cli_principal(),
            target="example-proxy",
        )
        self.assertTrue(result["ok"], result)
        self.unqueue()

        self.assert_applied("example-proxy")

    def test_a_create(self):
        result = execute_capability(
            "infrastructure.resource.create",
            {"key": "example-proxy", "kind": "npm.proxy_host", "spec": dict(PROXY)},
            principal=cli_principal(),
        )
        self.assertTrue(result["ok"], result)
        self.unqueue()

        self.assert_applied("example-proxy")

    def test_a_certificate_upload(self):
        ManagedResource.objects.create(
            key="example-uploaded",
            kind="tls.uploaded_certificate",
            spec={"certificate_name": "example-uploaded", "install_on": ["example-npm"]},
        )
        self.settle()
        fullchain, private_key = a_certificate("app.example.com")

        store_certificate(
            UploadCertificateCommand(fullchain=fullchain, private_key=private_key),
            principal=cli_principal(),
            current_key="example-uploaded",
        )
        self.unqueue()

        self.assert_applied("example-uploaded")

    def test_a_target_edit_that_moves_the_certificates_installed_there(self):
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(key="example-wildcard", kind="tls.certificate", spec=certificate_spec()),
            principal=cli_principal(),
        )
        self.settle()
        target = ManagedResource.objects.get(key="edge-certificate-target")
        before = ManagedResource.objects.get(key="example-wildcard").generation

        save_managed_resource(
            ManagedResourceCommand(
                key=target.key, kind=target.kind, spec={**target.spec, "certificate_directory": "/srv/certs"}
            ),
            principal=cli_principal(),
            current_key=target.key,
        )
        self.unqueue()

        self.assertGreater(ManagedResource.objects.get(key="example-wildcard").generation, before)
        self.assert_applied("example-wildcard")


class OnlyWhatNeedsApplyingIsDueTests(_Estate):
    def test_an_applied_record_is_no_pass(self):
        self.proxy()
        self.settle()

        self.assertEqual(cadence.pass_due(), "")

    def test_a_reconcile_queued_for_the_generation_is_not_asked_for_again_once_it_failed(self):
        """The controller's own schedule retries a failure; ringing every minute would not help it."""

        self.proxy()
        schedule_automatic_operations("example-controller")
        OperationRequest.objects.update(state=OperationRequest.State.FAILED, completed_at=timezone.now())

        self.assertEqual(cadence.pass_due(), "")

    def test_a_renewal_queued_for_the_generation_is_not_asked_for_again_once_it_failed(self):
        """A certificate in its renewal window is renewed rather than reconciled,
        and the renewal applies its settings as a reconcile would."""

        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(key="example-wildcard", kind="tls.certificate", spec=certificate_spec()),
            principal=cli_principal(),
        )
        self.settle()
        certificate = ManagedResource.objects.get(key="example-wildcard")
        certificate.status = {"not_after": (timezone.now() + timedelta(days=10)).isoformat()}
        certificate.generation += 1
        certificate.save(update_fields=("status", "generation"))

        schedule_automatic_operations("example-controller")
        renewal = OperationRequest.objects.get(resource=certificate)
        self.assertEqual(renewal.action, OperationRequest.Action.RENEW)
        renewal.state = OperationRequest.State.FAILED
        renewal.completed_at = timezone.now()
        renewal.save(update_fields=("state", "completed_at"))

        self.assertEqual(cadence.pass_due(), "")

    def test_a_record_switched_off_is_no_pass(self):
        save_managed_resource(
            ManagedResourceCommand(key="example-proxy", kind="npm.proxy_host", spec=dict(PROXY), enabled=False),
            principal=cli_principal(),
        )

        self.assertEqual(cadence.pass_due(), "")

    def test_a_kind_applied_only_when_asked_is_no_pass(self):
        save_managed_resource(
            ManagedResourceCommand(
                key="a-domain", kind="cloudflare.zone", spec={"zone": "example.com", "connection_ref": "cf-example"}
            ),
            principal=cli_principal(),
        )

        self.assertEqual(cadence.pass_due(), "")

    def test_asking_does_not_grow_with_the_estate(self):
        """Asked once a minute and after every write, so its cost may not follow the record count."""

        def asked():
            with CaptureQueriesContext(connection) as queries:
                cadence.pass_due()
            return len(queries)

        for index in range(3):
            self.proxy(f"example-proxy-{index}", forward_port=8000 + index)
        self.unqueue()
        few = asked()
        for index in range(3, 30):
            self.proxy(f"example-proxy-{index}", forward_port=8000 + index)
        self.unqueue()

        self.assertEqual(asked(), few)


class AWriteStartsAPassAtOnceTests(_Estate):
    """A write through a capability rings as it commits, rather than at the next minute's ask."""

    def test_a_write_that_moves_another_records_generation_rings(self):
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(key="example-wildcard", kind="tls.certificate", spec=certificate_spec()),
            principal=cli_principal(),
        )
        self.settle()
        target = ManagedResource.objects.get(key="edge-certificate-target")

        with self.captureOnCommitCallbacks(execute=True):
            result = execute_capability(
                "infrastructure.resource.update",
                {"key": target.key, "kind": target.kind, "spec": {**target.spec, "certificate_directory": "/srv/c"}},
                principal=cli_principal(),
                target=target.key,
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(self.doorbell.exists())

    def test_a_read_rings_nothing_even_when_a_pass_is_due(self):
        self.proxy()
        self.unqueue()
        self.assertTrue(cadence.pass_due())

        with self.captureOnCommitCallbacks(execute=True):
            result = execute_capability("contact.submissions.list", {}, principal=cli_principal())

        self.assertTrue(result["ok"], result)
        self.assertFalse(self.doorbell.exists())

    def test_a_write_with_nothing_to_apply_rings_nothing(self):
        self.proxy()
        self.settle()

        with self.captureOnCommitCallbacks(execute=True):
            result = execute_capability(
                "infrastructure.resource.update",
                {"key": "example-proxy", "kind": "npm.proxy_host", "spec": dict(PROXY)},
                principal=cli_principal(),
                target="example-proxy",
            )

        self.assertTrue(result["ok"], result)
        self.assertFalse(self.doorbell.exists())
