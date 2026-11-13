"""Each host resource types every read it serves, and each answer is of its type.

A list's items are its row and a read of one its record, both closed as the
API document publishes them, so a key an answer holds that its type does not
name fails here as much as one the type requires and the answer lacks. The
answers are read on the data set the showcase serves, as JSON leaves HQ.
"""

import json
from datetime import timedelta
from typing import Any, TypedDict

from django.core.exceptions import ImproperlyConfigured
from django.core.serializers.json import DjangoJSONEncoder
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from jsonschema import Draft202012Validator

from hq.domains.calendars.models import Entry
from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application.automation_adoption import catch_up
from hq.platform.application.field_masks import WHOLE_RECORD
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.integration_validation import validate_resource_spec
from hq.platform.application.json_schemas import json_schema
from hq.platform.application.plugins import plugin_resource_specs
from hq.platform.application.resources import (
    BoundedQuery,
    get_resource,
    list_resource,
    resource_registry,
)
from hq.platform.application.security import Capability, Principal
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

READER = Principal("example-reader", "api", frozenset(Capability))
# What a list narrows by where its default answers nothing.
QUERIES = {"search": {"query": "example"}}


class _ExampleNote(TypedDict):
    slug: str
    title: str


def _notes(*, limit: int = 50) -> dict[str, Any]:
    return {"items": [], "count": 0}


def _validator(kind: Any) -> Draft202012Validator:
    return Draft202012Validator(json_schema(kind, mode="serialization"))


def _sent(value: Any) -> Any:
    """``value`` as the API sends it."""

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _host_resources() -> dict[str, ResourceSpec]:
    """The registry's resources less those an installed extension provides."""

    provided = {spec.name for spec in plugin_resource_specs()}
    return {name: spec for name, spec in resource_registry().items() if name not in provided}


def _spec(**types: Any) -> ResourceSpec:
    return ResourceSpec("example.notes", "Notes", "Example notes.", "example.read", _notes, BoundedQuery, **types)


class RecordTypeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase"):
            pass

    def test_every_host_resource_types_each_read_it_serves(self):
        for name, spec in _host_resources().items():
            with self.subTest(resource=name):
                if spec.list_handler:
                    self.assertIsNotNone(spec.row_type)
                if spec.detail_handler:
                    self.assertIsNotNone(spec.record_type)

    def test_each_answer_is_of_its_resources_type(self):
        # Read past the doors, which each read the playbooks adopted now as they serve a call.
        catch_up()
        registry = _host_resources()
        identifiers: dict[str, list[Any]] = {}
        for name, spec in registry.items():
            if not spec.list_handler:
                continue
            query = QUERIES.get(name, {})
            row = _validator(spec.row_type)
            for item in list_resource(name, query, principal=READER)["items"]:
                with self.subTest(resource=name, item=item.get(spec.identifier or "")):
                    row.validate(_sent(item))
            if spec.identifier:
                whole = list_resource(name, query, principal=READER, fields=[WHOLE_RECORD])["items"]
                identifiers[name] = [item[spec.identifier] for item in whole[:3] if spec.identifier in item]
        # The reads of one no list holds, by what names their records elsewhere.
        identifiers["relationships"] = [
            f"machine:{identifiers['machines'][0]}",
            f"zone:{identifiers['domains'][0]}",
        ]
        identifiers["service.paths"] = identifiers["services"][:2]
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
            if not spec.detail_handler:
                continue
            record = _validator(spec.record_type)
            for identifier in identifiers.get(name, ()):
                with self.subTest(resource=name, identifier=identifier):
                    record.validate(_sent(get_resource(name, identifier, principal=READER)))
                    read.add(name)
        # Each read of one was checked on a real record, so none passes unread.
        self.assertEqual(read, {name for name, spec in registry.items() if spec.detail_handler})

    def test_a_list_keeps_its_rows_keys_unless_asked_for_more(self):
        spec = resource_registry()["machines"]
        page = list_resource("machines", {}, principal=READER)
        whole = list_resource("machines", {}, principal=READER, fields=[WHOLE_RECORD])

        self.assertTrue(page["items"])
        for item in page["items"]:
            self.assertLessEqual(set(item), set(spec.list_fields))
        self.assertGreater(set(whole["items"][0]), set(spec.list_fields))


class RecordTypeContractTests(SimpleTestCase):
    def test_a_rows_keys_are_its_lists_fields(self):
        spec = _spec(row_type=_ExampleNote)

        validate_resource_spec(spec)
        self.assertEqual(spec.list_fields, ("slug", "title"))

    def test_a_type_that_is_no_typed_dict_is_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, "TypedDict"):
            validate_resource_spec(_spec(row_type=dict))

    def test_a_record_for_a_read_the_resource_does_not_serve_is_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, "does not serve"):
            validate_resource_spec(_spec(record_type=_ExampleNote))

    def test_list_fields_other_than_the_rows_keys_are_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, "row_type's keys"):
            validate_resource_spec(_spec(list_fields=("title",), row_type=_ExampleNote))
