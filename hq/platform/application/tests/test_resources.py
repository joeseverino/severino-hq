"""Contract tests for HQ's shared read-resource registry."""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project

from ..command_center import command_center
from ..integrations import integration_graph
from ..plugins import plugin_resource_specs
from ..resources import (
    EmptyQuery,
    InvalidResourceInput,
    ResourceSpec,
    describe_resources,
    get_resource,
    list_resource,
)
from ..security import AuthorizationError, Capability, Principal

READ = Principal("reader", "test", frozenset({Capability.READ}))
NONE = Principal("nobody", "test", frozenset())
OPERATOR = Principal("operator", "test", frozenset(Capability))


class ResourceExecutionTests(TestCase):
    def test_one_spec_drives_discovery_list_and_detail(self):
        project = Project.objects.create(name="Resource registry")

        described = describe_resources()
        spec = next(item for item in described["resources"] if item["name"] == "projects")
        listed = list_resource("projects", {"query": "registry", "limit": 10}, principal=READ)

        self.assertEqual(spec["operations"]["get"]["identifier"], "slug")
        self.assertFalse(spec["operations"]["list"]["query_schema"]["additionalProperties"])
        self.assertEqual(listed["items"][0]["slug"], project.slug)
        self.assertEqual(
            get_resource("projects", project.slug, principal=READ)["slug"],
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
            list_handler=lambda: {"items": [], "count": 0},
            list_query_type=EmptyQuery,
            web_route="missing:list",
        )
        installed = plugin_resource_specs()
        with mock.patch(
            "hq.platform.application.plugins.plugin_resource_specs",
            return_value=(*installed, plugin),
        ):
            outcome = command_center("example.records", principal=OPERATOR)

        self.assertEqual(outcome["resources"][0].url, "")

    def test_command_center_omits_things_the_principal_cannot_use(self):
        outcome = command_center("", principal=NONE)

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

    def test_unknown_filters_are_rejected_before_the_handler(self):
        with self.assertRaises(InvalidResourceInput) as refused:
            list_resource("projects", {"limti": 10}, principal=READ)

        self.assertEqual(refused.exception.reason, "projects: limti is not a known field.")

    def test_infrastructure_kinds_filter_before_the_shared_page_bound(self):
        # Rows of other kinds come first, so a bound taken before the filter
        # would leave nothing to filter.
        ManagedResource.objects.create(key="zone", kind="cloudflare.zone", spec={})
        ManagedResource.objects.create(key="other-zone", kind="cloudflare.zone", spec={})
        ManagedResource.objects.create(key="device", kind="tailscale.device", spec={})
        ManagedResource.objects.create(key="policy", kind="tailscale.policy", spec={})

        listed = list_resource(
            "infrastructure.resources",
            {"kinds": "tailscale.device,tailscale.policy", "limit": 2},
            principal=READ,
        )

        self.assertEqual({item["key"] for item in listed["items"]}, {"device", "policy"})

    def test_infrastructure_kinds_filter_rejects_invalid_or_repeated_kinds(self):
        for value in ("not a kind", "tailscale.device,tailscale.device"):
            with self.subTest(value=value), self.assertRaises(InvalidResourceInput):
                list_resource("infrastructure.resources", {"kinds": value}, principal=READ)

    def test_every_operation_authorizes_before_reading(self):
        with self.assertRaises(AuthorizationError):
            list_resource("projects", {}, principal=NONE)

    def test_fields_keep_only_the_named_keys_of_a_record_and_of_each_item(self):
        project = Project.objects.create(name="Selected fields")
        Project.objects.create(name="Selected sibling")

        whole = get_resource("projects", project.slug, principal=READ)
        detail = get_resource("projects", project.slug, principal=READ, fields=["slug", "name"])
        listed = list_resource("projects", {"query": "selected"}, principal=READ, fields=("slug",))

        unselected = list_resource("projects", {"query": "selected"}, principal=READ)
        self.assertEqual(detail, {"slug": whole["slug"], "name": whole["name"]})
        self.assertEqual(listed["count"], 2)
        self.assertEqual({**listed, "items": unselected["items"]}, unselected)
        self.assertEqual(listed["items"], [{"slug": item["slug"]} for item in unselected["items"]])

    def test_no_fields_answers_the_whole_record(self):
        project = Project.objects.create(name="Whole record")

        self.assertEqual(
            get_resource("projects", project.slug, principal=READ, fields=None),
            get_resource("projects", project.slug, principal=READ),
        )

    def test_a_field_no_record_carries_is_refused_with_the_keys_there_are(self):
        project = Project.objects.create(name="Refused field")

        for read in (
            lambda fields: get_resource("projects", project.slug, principal=READ, fields=fields),
            lambda fields: list_resource("projects", {}, principal=READ, fields=fields),
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
            lambda: get_resource("projects", project.slug, principal=NONE, fields=["no-such-key"]),
            lambda: list_resource("projects", {}, principal=NONE, fields=["no-such-key"]),
        ):
            with self.subTest(read=read), self.assertRaises(AuthorizationError):
                read()

    def test_a_reading_answers_its_age_and_each_records_waiting_and_nothing_else(self):
        import json

        from ..security import cli_principal
        from ..sweep import record_sweep

        bulk = [{"name": f"image-{n}", "digest": "sha256:" + "0" * 64} for n in range(40)]
        record_sweep(
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
            principal=cli_principal(),
            controller_id="controller",
        )

        whole = get_resource("readings", "github.repository", principal=READ)
        selected = get_resource(
            "readings", "github.repository", principal=READ, fields=["observed_at", "items.waiting"]
        )

        self.assertEqual(
            selected,
            {"observed_at": whole["observed_at"], "items": [{"waiting": item["waiting"]} for item in whole["items"]]},
        )
        self.assertEqual(len(selected["items"]), 3)
        self.assertLess(len(json.dumps(selected)) * 20, len(json.dumps(whole)))


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
    lambda: {"items": [_nested(None), {"name": "flat"}], "count": 2},
    EmptyQuery,
    _nested,
    "name",
)


class FieldMaskTests(TestCase):
    def setUp(self):
        installed = plugin_resource_specs()
        patcher = mock.patch("hq.platform.application.plugins.plugin_resource_specs", return_value=(*installed, NESTED))
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, *fields):
        return get_resource("example.nested", "example", principal=READ, fields=list(fields))

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
        listed = list_resource("example.nested", {}, principal=READ, fields=["name", "items.name"])

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
            list_handler=_no_rows,
            list_query_type=EmptyQuery,
        )
        with (
            mock.patch(
                "hq.platform.application.plugins.plugin_resource_specs",
                return_value=(duplicate,),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "Duplicate resource name"),
        ):
            integration_graph()

    def test_incomplete_list_contract_fails_at_registry_construction(self):
        invalid = ResourceSpec(
            "example.records",
            "Records",
            "A deliberately incomplete plugin contract.",
            "example.read",
            list_handler=_no_rows,
        )
        with (
            mock.patch("hq.platform.application.plugins.plugin_resource_specs", return_value=(invalid,)),
            self.assertRaisesRegex(ImproperlyConfigured, "handler and query together"),
        ):
            integration_graph()

    def test_a_query_that_declares_fields_fails_at_registry_construction(self):
        from pydantic import BaseModel, ConfigDict, Field

        class Named(BaseModel):
            model_config = ConfigDict(extra="forbid")
            fields: str = ""

        class Aliased(BaseModel):
            model_config = ConfigDict(extra="forbid")
            keys: str = Field("", alias="fields")

        for query in (Named, Aliased):
            spec = ResourceSpec(
                "example.records",
                "Records",
                "A query that takes the selection's name.",
                Capability.READ,
                list_handler=lambda **_: {"items": [], "count": 0},
                list_query_type=query,
            )
            with (
                self.subTest(query=query.__name__),
                mock.patch("hq.platform.application.plugins.plugin_resource_specs", return_value=(spec,)),
                self.assertRaisesRegex(ImproperlyConfigured, "may not declare 'fields'"),
            ):
                integration_graph()

    def test_nested_django_namespaces_are_valid_web_routes(self):
        nested = ResourceSpec(
            "example.records",
            "Records",
            "A nested plugin route.",
            Capability.READ,
            list_handler=lambda: {"items": [], "count": 0},
            list_query_type=EmptyQuery,
            web_route="example:records:list",
        )
        installed = plugin_resource_specs()
        with mock.patch(
            "hq.platform.application.plugins.plugin_resource_specs",
            return_value=(*installed, nested),
        ):
            self.assertIn(nested, integration_graph().resources.values())


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

        ManagedResource.objects.create(key="box", kind="machine", spec={"name": "box"})

        # No suffix: the collision is gone, so nothing has to be invented.
        self.assertEqual(suggest_key("tailscale.device", {"name": "box"}), "box-tailnet")

    def test_the_machine_still_keys_on_its_plain_name(self):
        from hq.platform.application.infrastructure import suggest_key

        self.assertEqual(suggest_key("machine", {"name": "box"}), "box")
