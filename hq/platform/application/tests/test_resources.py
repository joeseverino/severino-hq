"""Contract tests for HQ's shared read-resource registry."""

from typing import Any, NotRequired, TypedDict

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from hq.domains.projects.models import Project
from hq.platform.application.contracts import Closed
from hq.platform.application.projection import page_of
from hq.platform.core.testing import NOBODY, OPERATOR, READER, declare

from ..command_center import command_center
from ..integration_specs import ListRead
from ..integrations import held_graph
from ..plugin_testing import offering
from ..record_errors import RecordNotFound
from ..report_testing import bridged
from ..resources import (
    EmptyQuery,
    InvalidResourceInput,
    ResourceSpec,
    describe_resources,
    get_resource,
    list_resource,
)
from ..security import AuthorizationError, Capability

# Distinctive enough that finding it in a message can only mean it was echoed.
SENT = "sent-by-the-caller-7f3a"


class ResourceExecutionTests(TestCase):
    def test_one_spec_drives_discovery_list_and_detail(self):
        project = Project.objects.create(name="Resource registry")

        (spec,) = describe_resources(name="projects")["resources"]
        listed = list_resource("projects", {"query": "registry", "limit": 10}, principal=READER)

        self.assertEqual(spec["operations"]["get"]["identifier"], "slug")
        self.assertFalse(spec["operations"]["list"]["query_schema"]["additionalProperties"])
        self.assertEqual(listed["items"][0]["slug"], project.slug)
        self.assertEqual(
            get_resource("projects", project.slug, principal=READER)["slug"],
            project.slug,
        )

    def test_command_center_derives_links_and_operations_from_the_registries(self):
        with self.assertNumQueries(0):
            outcome = command_center("certificate.renew", principal=OPERATOR)
        renewal = next(item for item in outcome["commands"] if item.name == "certificate.renew")

        self.assertEqual(renewal.url, "/commands/certificate.renew/")
        self.assertEqual(renewal.destination_label, "")
        self.assertEqual(renewal.badges, ("Changes a live system",))

    def test_command_center_degrades_an_unusable_plugin_route_to_text(self):
        plugin = ResourceSpec(
            "example.records",
            "Example records",
            "Synthetic plugin records.",
            Capability.READ,
            listing=ListRead(lambda: page_of([]), EmptyQuery, _ExampleRecord),
            web_route="missing:list",
        )
        with offering(resources=(plugin,)):
            outcome = command_center("example.records", principal=OPERATOR)

        self.assertEqual(outcome["resources"][0].url, "")

    def test_command_center_omits_things_the_principal_cannot_use(self):
        outcome = command_center("", principal=NOBODY)

        self.assertEqual(
            outcome,
            {
                "estate": (),
                "resources": (),
                "commands": (),
                "connections": (),
                "views": (),
                "checks": (),
            },
        )

    def test_infrastructure_kinds_filter_before_the_shared_page_bound(self):
        # Rows of other kinds come first, so a bound taken before the filter
        # would leave nothing to filter.
        declare("zone", "cloudflare.zone")
        declare("other-zone", "cloudflare.zone")
        declare("device", "tailscale.device")
        declare("policy", "tailscale.policy")

        listed = list_resource(
            "infrastructure",
            {"kinds": "tailscale.device,tailscale.policy", "limit": 2},
            principal=READER,
        )

        self.assertEqual({item["key"] for item in listed["items"]}, {"device", "policy"})

    def test_infrastructure_kinds_filter_rejects_a_kind_it_does_not_list(self):
        for value in ("not a kind", "tailscale.device,not.a.kind", "tailscale.device,"):
            with self.subTest(value=value), self.assertRaises(InvalidResourceInput):
                list_resource("infrastructure", {"kinds": value}, principal=READER)

    def test_every_operation_authorizes_before_reading(self):
        with self.assertRaises(AuthorizationError):
            list_resource("projects", {}, principal=NOBODY)

    def test_fields_keep_only_the_named_keys_of_a_record_and_of_each_item(self):
        project = Project.objects.create(name="Selected fields")
        Project.objects.create(name="Selected sibling")

        whole = get_resource("projects", project.slug, principal=READER)
        detail = get_resource("projects", project.slug, principal=READER, fields=["slug", "name"])
        listed = list_resource("projects", {"query": "selected"}, principal=READER, fields=("slug",))

        unselected = list_resource("projects", {"query": "selected"}, principal=READER)
        self.assertEqual(detail, {"slug": whole["slug"], "name": whole["name"]})
        self.assertEqual(listed["count"], 2)
        self.assertEqual({**listed, "items": unselected["items"]}, unselected)
        self.assertEqual(listed["items"], [{"slug": item["slug"]} for item in unselected["items"]])

    def test_no_fields_answers_the_whole_record(self):
        project = Project.objects.create(name="Whole record")

        self.assertEqual(
            get_resource("projects", project.slug, principal=READER, fields=None),
            get_resource("projects", project.slug, principal=READER),
        )

    def test_a_field_no_record_carries_is_refused_with_the_keys_there_are(self):
        project = Project.objects.create(name="Refused field")

        for read in (
            lambda fields: get_resource("projects", project.slug, principal=READER, fields=fields),
            lambda fields: list_resource("projects", {}, principal=READER, fields=fields),
        ):
            with self.subTest(read=read), self.assertRaises(InvalidResourceInput) as refused:
                read(["slug", "slgu"])
            self.assertIn("fields[1] must be one of", refused.exception.reason)
            self.assertIn("slug", refused.exception.reason.split("one of", 1)[1])
            with self.assertRaises(InvalidResourceInput):
                read([])

    def test_authorization_comes_before_selection(self):
        project = Project.objects.create(name="Unread")

        for read in (
            lambda: get_resource("projects", project.slug, principal=NOBODY, fields=["no-such-key"]),
            lambda: list_resource("projects", {}, principal=NOBODY, fields=["no-such-key"]),
        ):
            with self.subTest(read=read), self.assertRaises(AuthorizationError):
                read()

    def test_a_reading_answers_its_age_and_each_records_waiting_and_nothing_else(self):
        import json

        bulk = [{"name": f"image-{n}", "digest": "sha256:" + "0" * 64} for n in range(40)]
        bridged(
            "inventory",
            {
                "github.repository": {
                    "ok": True,
                    "records": [
                        {
                            "connection_ref": "example-github",
                            "repository": f"example/repo-{n}",
                            "waiting": [{"environment": "production"}] if n == 0 else [],
                            "images": bulk,
                            "artifacts": bulk,
                        }
                        for n in range(3)
                    ],
                }
            },
            controller_id="controller",
        )

        whole = get_resource("readings", "github.repository", principal=READER)
        selected = get_resource(
            "readings", "github.repository", principal=READER, fields=["observed_at", "items.waiting"]
        )

        self.assertEqual(
            selected,
            {"observed_at": whole["observed_at"], "items": [{"waiting": item["waiting"]} for item in whole["items"]]},
        )
        self.assertEqual(len(selected["items"]), 3)
        self.assertLess(len(json.dumps(selected)) * 20, len(json.dumps(whole)))


class _ExampleRecord(TypedDict):
    name: str
    state: NotRequired[str]
    trace: NotRequired[None]
    items: NotRequired[list[dict[str, Any]]]


def _nested(_identifier):
    return {
        "name": "example",
        "state": "good",
        "trace": None,
        "items": [
            {"name": "one", "head": {"sha": "a", "message": "m"}, "tags": ["x"]},
            {"name": "two", "head": {"sha": "b", "message": "n"}, "extra": 1},
        ],
    }


NESTED = ResourceSpec(
    "example.nested",
    "Nested",
    "Synthetic records with collections inside.",
    Capability.READ,
    listing=ListRead(lambda: page_of([_nested(None), {"name": "flat"}]), EmptyQuery, _ExampleRecord),
    detail_handler=_nested,
    identifier="name",
)


class FieldMaskTests(TestCase):
    def setUp(self):
        self.enterContext(offering(resources=(NESTED,)))

    def get(self, *fields):
        return get_resource("example.nested", "example", principal=READER, fields=list(fields))

    def refusal(self, *fields):
        with self.assertRaises(InvalidResourceInput) as refused:
            self.get(*fields)
        return refused.exception.reason

    def test_a_path_through_a_list_cuts_every_element_and_shared_prefixes_merge(self):
        self.assertEqual(
            self.get("name", "items.name", "items.head.sha"),
            {
                "name": "example",
                "items": [{"name": "one", "head": {"sha": "a"}}, {"name": "two", "head": {"sha": "b"}}],
            },
        )

    def test_a_whole_value_wins_over_a_path_into_it(self):
        whole = self.get("items")
        self.assertEqual(self.get("items.name", "items"), whole)
        self.assertEqual(self.get("items", "items.name"), whole)

    def test_a_name_any_element_carries_is_valid_and_a_null_stays_null(self):
        self.assertEqual(self.get("items.extra", "trace.anything"), {"trace": None, "items": [{}, {"extra": 1}]})

    def test_an_unknown_segment_is_refused_with_the_names_at_its_depth(self):
        reason = self.refusal("name", "items.head.shaa")

        self.assertIn("fields[1] must be one of", reason)
        self.assertIn("sha, message", reason)
        self.assertNotIn("tags", reason)

    def test_a_path_into_a_scalar_or_a_list_of_scalars_is_refused(self):
        for path, where in (("state.x", "fields[0].state"), ("items.tags.x", "fields[0].items.tags")):
            with self.subTest(path):
                self.assertIn(f"{where} must be a valid dictionary", self.refusal(path))

    def test_a_malformed_path_is_refused(self):
        for path in ("items.", ".name", "items..name"):
            with self.subTest(path):
                self.assertIn("fields[0]", self.refusal(path))

    def test_on_a_list_a_path_is_read_in_each_item(self):
        listed = list_resource("example.nested", {}, principal=READER, fields=["name", "items.name"])

        self.assertEqual(listed["count"], 2)
        self.assertEqual(
            listed["items"], [{"name": "example", "items": [{"name": "one"}, {"name": "two"}]}, {"name": "flat"}]
        )


def _no_rows():
    # A function and not the builtin dict: registration reads the handler's signature.
    return {}


class ResourceRegistrationTests(SimpleTestCase):
    def test_plugin_resource_names_cannot_shadow_core(self):
        duplicate = ResourceSpec(
            "projects",
            "Duplicate",
            "Must fail before an adapter silently wins by ordering.",
            Capability.READ,
            listing=ListRead(_no_rows, EmptyQuery, _ExampleRecord),
        )
        with (
            offering(resources=(duplicate,)),
            self.assertRaisesRegex(ImproperlyConfigured, "Duplicate resource name"),
        ):
            held_graph()

    def test_a_query_that_declares_fields_fails_at_registry_construction(self):
        from pydantic import Field

        class Named(Closed):
            fields: str = ""

        class Aliased(Closed):
            keys: str = Field("", alias="fields")

        for query in (Named, Aliased):
            spec = ResourceSpec(
                "example.records",
                "Records",
                "A query that takes the selection's name.",
                Capability.READ,
                listing=ListRead(lambda **_: page_of([]), query, _ExampleRecord),
            )
            with (
                self.subTest(query=query.__name__),
                offering(resources=(spec,)),
                self.assertRaisesRegex(ImproperlyConfigured, "may not declare 'fields'"),
            ):
                held_graph()

    def test_a_query_open_to_any_filter_fails_at_registry_construction(self):
        from pydantic import BaseModel

        class Open(BaseModel):
            status: str = ""

        spec = ResourceSpec(
            "example.records",
            "Records",
            "A query that drops a filter it does not declare.",
            Capability.READ,
            listing=ListRead(lambda **_: page_of([]), Open, _ExampleRecord),
        )
        with (
            offering(resources=(spec,)),
            self.assertRaisesRegex(ImproperlyConfigured, "must refuse a filter it does not declare"),
        ):
            held_graph()

    def test_nested_django_namespaces_are_valid_web_routes(self):
        nested = ResourceSpec(
            "example.records",
            "Records",
            "A nested plugin route.",
            Capability.READ,
            listing=ListRead(lambda: page_of([]), EmptyQuery, _ExampleRecord),
            web_route="example:records:list",
        )
        with offering(resources=(nested,)):
            self.assertIn(nested, held_graph().resources.values())


class TailnetDeviceKeyTests(TestCase):
    """A machine and its tailnet device must not compete for one key. Keyed on
    the same bare device name, the second declaration adopted would be filed as
    `<name>-2`: a suffix recording arrival order on an estate where the name is
    what everything else joins on.
    """

    def test_a_tailnet_device_key_says_what_it_is(self):
        from hq.platform.application.infrastructure import suggest_key

        self.assertEqual(suggest_key("tailscale.device", {"name": "box"}), "box-tailnet")

    def test_it_does_not_collide_with_the_machine_of_the_same_name(self):
        from hq.platform.application.infrastructure import suggest_key

        declare("box", "machine", {"name": "box"})

        # No suffix: the collision is gone, so nothing has to be invented.
        self.assertEqual(suggest_key("tailscale.device", {"name": "box"}), "box-tailnet")

    def test_the_machine_still_keys_on_its_plain_name(self):
        from hq.platform.application.infrastructure import suggest_key

        self.assertEqual(suggest_key("machine", {"name": "box"}), "box")


class MissingRecordReadTests(TestCase):
    def test_every_host_resource_answers_a_missing_record_as_not_found(self):
        cases = {
            "infrastructure": SENT,
            "services": f"not a host {SENT}",
            "machines": SENT,
            "containers": f"{SENT}:{SENT}",
            "upgrades": f"{SENT}:{SENT}",
            "domains": f"{SENT}.example",
            "relationships": f"machine:{SENT}",
            "service.paths": f"not a host {SENT}",
            "readings": SENT,
            "credentials": SENT,
            "connections": SENT,
            "contact.submissions": 7_340_219,
        }
        for name, identifier in cases.items():
            with self.subTest(name):
                with self.assertRaises(RecordNotFound) as refused:
                    get_resource(name, identifier, principal=OPERATOR, strict=False)
                self.assertNotIn(SENT, str(refused.exception))
                self.assertNotIn(str(identifier), str(refused.exception))
