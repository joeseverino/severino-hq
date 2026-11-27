"""A command field holding a reference offers a kind's records only to who may name them."""

from unittest import mock

from django.test import TestCase

from hq.domains.projects.models import Project
from hq.platform.application import references
from hq.platform.application.capabilities import command_spec
from hq.platform.application.command_targets import record_options
from hq.platform.application.integration_specs import capability_schema
from hq.platform.application.references import Referable, Target
from hq.platform.application.security import Principal


class GuardedReferenceTests(TestCase):
    def test_a_kind_is_offered_only_to_who_may_name_it(self):
        Project.objects.create(name="An Example Account", slug="an-account")
        guarded = Target(
            "example.account",
            Project,
            Referable(key="slug", shows=("name",), role="account", requires="example.read"),
        )
        self.enterContext(mock.patch.object(references, "targets", lambda: {"example.account": guarded}))
        schema = capability_schema(command_spec("expense.create"))
        reader = Principal("a reader", "web", frozenset({"read", "example.read"}))
        stranger = Principal("a stranger", "web", frozenset({"read"}))

        read = record_options(schema, principal=reader).get("paid_from")
        withheld = record_options(schema, principal=stranger).get("paid_from")

        self.assertEqual([option.value for option in read or ()], ["example.account:an-account"])
        self.assertEqual(withheld, ())
