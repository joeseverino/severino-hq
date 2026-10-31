"""A declaration is written only once the capability runner authorized its caller,
and as that caller last read it."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource, OperationRequest

from ..adoption_testing import managing_everything
from ..capabilities import execute_capability
from ..infrastructure import ManagedResourceCommand, save_managed_resource
from ..security import AuthorizationError, Capability, Principal, cli_principal
from ..sweep import record_sweep

PROXY = {
    "domain_names": ["app.example.com"],
    "forward_scheme": "http",
    "forward_host": "192.0.2.10",
    "forward_port": 8000,
}

# Every capability whose handler writes a declaration through
# save_managed_resource, with a target where it takes one.
WRITERS = (
    ("infrastructure.resource.create", None),
    ("infrastructure.resource.update", "example-proxy"),
    ("infrastructure.resource.adopt", None),
    ("infrastructure.service.adopt", None),
    ("infrastructure.zone.adopt", None),
    ("infrastructure.resource.accept_observed", "example-proxy"),
    ("tailnet.reach.allow", "example-proxy"),
    ("tailnet.policy.remove_empty_groups", "example-proxy"),
)

READER = Principal("example-reader", "cli", frozenset({Capability.READ}))


class _Declared(TestCase):
    def setUp(self):
        managing_everything()
        save_managed_resource(
            ManagedResourceCommand(key="example-proxy", kind="npm.proxy_host", spec=dict(PROXY)),
            principal=cli_principal(),
        )
        OperationRequest.objects.all().delete()

    def update(self, key="example-proxy", *, expected_updated_at=None, **spec):
        return execute_capability(
            "infrastructure.resource.update",
            {"key": key, "kind": "npm.proxy_host", "spec": {**PROXY, **spec}},
            principal=cli_principal(),
            target=key,
            expected_updated_at=expected_updated_at,
        )

    def port(self):
        return ManagedResource.objects.get(key="example-proxy").spec["forward_port"]


class AuthorizedAtTheRunnerTests(_Declared):
    def test_every_writer_refuses_a_caller_without_infrastructure_authority_and_writes_nothing(self):
        before = list(ManagedResource.objects.values_list("key", "spec", "generation"))
        for name, target in WRITERS:
            with self.subTest(capability=name):
                result = execute_capability(name, {}, principal=READER, target=target)
                self.assertEqual(result["error"]["code"], "forbidden", result)
        self.assertEqual(list(ManagedResource.objects.values_list("key", "spec", "generation")), before)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_sweep_is_refused_before_it_adopts_anything(self):
        with self.assertRaises(AuthorizationError):
            record_sweep({}, principal=READER)
        self.assertEqual(ManagedResource.objects.count(), 1)

    def test_an_authorized_writer_saves(self):
        result = self.update(forward_port=8080)

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.port(), 8080)


class HeldAsLastReadTests(_Declared):
    def test_a_write_from_the_current_read_lands(self):
        read = ManagedResource.objects.get(key="example-proxy").updated_at.isoformat()

        result = self.update(expected_updated_at=read, forward_port=8080)

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.port(), 8080)

    def test_a_write_from_a_stale_read_is_refused_and_changes_nothing(self):
        result = self.update(expected_updated_at="2000-01-01T00:00:00+00:00", forward_port=8080)

        self.assertEqual(
            result, {"ok": False, "error": {"code": "conflict", "message": "That record changed after it was read."}}
        )
        self.assertEqual(self.port(), 8000)

    def test_a_write_to_a_record_that_is_gone_says_so(self):
        result = self.update("example-gone")

        self.assertEqual(result["error"]["message"], "That record was not found.")
        self.assertFalse(ManagedResource.objects.filter(key="example-gone").exists())
