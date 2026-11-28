"""Each resource types every read it serves, and each answer is of its type.

A list's whole items are its item type and a read of one its record, both
closed as the API document publishes them, so a key an answer holds that its
type does not name fails here as much as one the type requires and the answer
lacks. The answers are read on the data set the showcase serves, as JSON
leaves HQ.
"""

import json
from datetime import timedelta
from typing import Any, TypedDict

from django.core.exceptions import ImproperlyConfigured
from django.core.serializers.json import DjangoJSONEncoder
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from jsonschema import Draft202012Validator

from hq.domains.calendars.models import Entry
from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application.automation_adoption import catch_up
from hq.platform.application.field_masks import WHOLE_RECORD
from hq.platform.application.integration_specs import ListRead, ResourceSpec
from hq.platform.application.integration_validation import validate_resource_spec
from hq.platform.application.json_schemas import json_schema
from hq.platform.application.plugins import plugin_resource_specs
from hq.platform.application.projection import page_of
from hq.platform.application.resources import BoundedQuery, get_resource, list_resource, resource_registry
from hq.platform.application.security import Capability, Principal
from hq.platform.core import seeds
from hq.platform.core.example_estate.names import HQ_SOURCE
from hq.platform.core.example_estate.testing import throwaway_media

READER = Principal("example-reader", "api", frozenset(Capability))
# What a list narrows by where its default answers nothing.
QUERIES = {"search": {"query": "example"}}


class _ExampleNote(TypedDict):
    slug: str
    title: str


def _notes(*, limit: int = 50) -> dict[str, Any]:
    return page_of([])


def _validator(kind: Any) -> Draft202012Validator:
    return Draft202012Validator(json_schema(kind, mode="serialization"))


def _sent(value: Any) -> Any:
    """``value`` as the API sends it."""

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _host_resources() -> dict[str, ResourceSpec]:
    """The registry's resources less those an installed extension provides."""

    provided = {spec.name for spec in plugin_resource_specs()}
    return {name: spec for name, spec in resource_registry().items() if name not in provided}


def _spec(item: type = dict, fields: tuple[str, ...] = (), **types: Any) -> ResourceSpec:
    listing = ListRead(_notes, BoundedQuery, item, fields)
    return ResourceSpec("example.notes", "Notes", "Example notes.", "example.read", listing, **types)


# HQ's releases are those of the repository its build names.
@override_settings(SEVERINO_HQ_SOURCE=HQ_SOURCE)
class RecordTypeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase"):
            pass

    def test_each_answer_is_of_its_resources_type(self):
        # Read past the doors, which each read the playbooks adopted now as they serve a call.
        catch_up()
        registry = _host_resources()
        identifiers: dict[str, list[Any]] = {}
        for name, spec in registry.items():
            if not spec.listing:
                continue
            whole = list_resource(name, QUERIES.get(name, {}), principal=READER, fields=[WHOLE_RECORD])["items"]
            declared = _validator(spec.listing.item)
            for item in whole:
                with self.subTest(resource=name, item=item.get(spec.identifier or "")):
                    declared.validate(_sent(item))
            if spec.identifier:
                identifiers[name] = [item[spec.identifier] for item in whole[:3] if spec.identifier in item]
        # The reads of one no list holds, by what names their records elsewhere.
        identifiers["relationships"] = [
            f"machine:{identifiers['machines'][0]}",
            f"zone:{identifiers['domains'][0]}",
        ]
        identifiers["service.paths"] = identifiers["services"][:2]
        # A run lists among the work, by the id a read of one takes.
        identifiers["runs"] = [
            item["id"]
            for item in list_resource("work", {}, principal=READER, fields=[WHOLE_RECORD])["items"]
            if item["source"] == "run"
        ][:3]
        # The agenda lists every source's events; a read of one is of your own.
        identifiers["calendar"] = [str(uid) for uid in Entry.objects.values_list("uid", flat=True)[:3]]
        # A held request is read by its asker alone, so the reader asks one.
        held = ApprovalRequest.objects.create(
            capability="project.create",
            payload={"name": "Held"},
            content_fingerprint="0" * 64,
            requested_actor=READER.actor,
            requested_interface=READER.interface,
            requested_person_key=READER.person_key,
            expires_at=timezone.now() + timedelta(days=1),
        )
        identifiers["approvals"] = [str(held.pk)]
        read: set[str] = set()
        for name, spec in registry.items():
            if not spec.detail:
                continue
            record = _validator(spec.detail.record)
            for identifier in identifiers.get(name, ()):
                with self.subTest(resource=name, identifier=identifier):
                    record.validate(_sent(get_resource(name, identifier, principal=READER)))
                    read.add(name)
        # Each read of one was checked on a real record, so none passes unread.
        self.assertEqual(read, {name for name, spec in registry.items() if spec.detail})


class RecordTypeContractTests(SimpleTestCase):
    def test_a_type_that_is_no_typed_dict_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_resource_spec(_spec(dict))

    def test_fields_its_item_does_not_declare_are_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_resource_spec(_spec(_ExampleNote, ("body",)))
