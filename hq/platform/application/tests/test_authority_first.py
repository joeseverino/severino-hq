"""A capability call is authorized before HQ reads anything about its target."""

from unittest import mock

from django.test import TestCase

from hq_sdk.capabilities import StrictCommand

from ..capabilities import CapabilitySpec, execute_capability
from ..security import Capability, Principal


class _Nothing(StrictCommand):
    pass


class AuthorityFirstTests(TestCase):
    def setUp(self):
        self.ran = []

    def spec(self, *, target_kind=None):
        return CapabilitySpec(
            "example.records.change",
            "Change example records.",
            "remote_write",
            Capability.MANAGE_INFRASTRUCTURE,
            _Nothing,
            lambda command, **bound: self.ran.append(bound) or {"ok": True},
            target_kind=target_kind,
        )

    def call(self, spec, *, target):
        reader = Principal("example-reader", "cli", frozenset({Capability.READ}))
        with mock.patch("hq.platform.application.capabilities.capability_registry", return_value={spec.name: spec}):
            return execute_capability(spec.name, {}, principal=reader, target=target)

    def test_a_caller_without_the_grant_is_refused_it_whatever_target_it_sent(self):
        for name, spec, target in (
            ("a target it lacks", self.spec(target_kind="slug"), None),
            ("a target it takes none of", self.spec(), "example"),
            ("a target of the wrong kind", self.spec(target_kind="integer"), "not-a-number"),
            ("a target that names nothing", self.spec(target_kind="slug"), "example-unknown"),
        ):
            with self.subTest(name):
                result = self.call(spec, target=target)

                self.assertEqual(result["error"]["code"], "forbidden", result)
        self.assertEqual(self.ran, [])
