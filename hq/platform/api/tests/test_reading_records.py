"""Any reading's records, listed and filtered by their own fields through
one resource, the same on the machine API and on MCP."""

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.github import WORKFLOW_RUN_KIND
from hq.domains.control_plane.observations.npm import REDIRECT_KIND
from hq.platform.application.tests.host_only import host_only

from ..testing import ContractClient
from .doors import Caller, at_the_door, each_door
from .tokens import _token

READER = Caller("example-automation", "read")
RECORDS = "reading.records"
RUNS = (
    {"connection_ref": "github", "repository": "example/hq", "branch": "main", "id": 7, "name": "CI"},
    {"connection_ref": "github", "repository": "example/hq", "branch": "dev", "id": 8, "name": "CI"},
    {"connection_ref": "github", "repository": "example/site", "branch": "main", "id": 9, "name": "CI"},
    # Read with no branch, so it holds the record model's default.
    {"connection_ref": "github", "repository": "example/site", "id": 10, "name": "Scan"},
)
REDIRECTS = (
    {"id": 1, "hostnames": ["old.example.com", "older.example.com"], "enabled": False},
    {"id": 2, "hostnames": ["www.example.com"]},
)


def _store(kind, records):
    kept, refused = OBSERVATIONS[kind].clean(records)
    assert not refused
    ProviderInventory.objects.create(kind=kind, records=kept, reachable=True, observed_at=timezone.now())


class ReadingRecordsEachDoorTests(TestCase):
    def setUp(self):
        at_the_door(self)
        self.enterContext(host_only())
        _store(WORKFLOW_RUN_KIND, RUNS)
        _store(REDIRECT_KIND, REDIRECTS)

    def _listed(self, door, **filters):
        answer = door("list_resource", name=RECORDS, filters=filters, fields=["kind", "record.id"])
        self.assertTrue(answer.get("ok", True), answer)
        return [(item["kind"], item["record"]["id"]) for item in answer["items"]]

    def _refused(self, door, **filters):
        answer = door("list_resource", name=RECORDS, filters=filters)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["code"], "invalid_input")
        return [(error["type"], error["loc"]) for error in answer["error"]["details"]["errors"]]

    def test_a_filter_its_kind_does_not_take_is_refused_with_the_one_to_send(self):
        runs = WORKFLOW_RUN_KIND
        for door in each_door(self, READER, "list_resource"):
            with door:
                self.assertEqual(
                    self._refused(door, kind=runs, where={"repos": "example/hq"}), [("misnamed", ["where", "repos"])]
                )
                self.assertEqual(
                    self._refused(door, kind=runs, where={"example_unknown": "x"}),
                    [("unfiltered", ["where", "example_unknown"])],
                )
                self.assertEqual(self._refused(door, kind=runs, where={"id": "seven"})[0][1], ["where", "id"])
                self.assertEqual(self._refused(door, where={"branch": "main"})[0][1], ["where"])

    def test_an_objects_member_is_sent_once_over_http(self):
        client = ContractClient()
        headers = {"Authorization": f"Bearer {_token(READER.client, scope=READER.scope)}"}
        path = f"/api/v2/resources/{RECORDS}/?kind={WORKFLOW_RUN_KIND}"
        for twice in (
            "&where=main&where[branch]=main",
            "&where[branch]=main&where=main",
            "&where[branch]=main&where[branch]=dev",
        ):
            with self.subTest(query=twice):
                response = client.get(path + twice, headers=headers)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_every_kinds_records_are_filtered_by_fields_of_their_own(self):
        from hq.platform.application.reading_records import record_filter

        runs = WORKFLOW_RUN_KIND
        for door in each_door(self, READER, "list_resource"):
            with door:
                where = {"repository": "example/hq", "branch": "main"}
                self.assertEqual(self._listed(door, kind=runs, where=where), [(runs, 7)])
                self.assertEqual(self._listed(door, kind=runs, where={"id": 9}), [(runs, 9)])
                self.assertEqual(self._listed(door, kind=REDIRECT_KIND, where={"enabled": True}), [(REDIRECT_KIND, 2)])
                # A field that lists values keeps the records holding the one given.
                self.assertEqual(
                    self._listed(door, kind=REDIRECT_KIND, where={"hostnames": "older.example.com"}),
                    [(REDIRECT_KIND, 1)],
                )
                self.assertEqual(len(self._listed(door, kind=runs)), len(RUNS))
                self.assertEqual(len(self._listed(door)), len(RUNS) + len(REDIRECTS))

        for kind, spec in OBSERVATIONS.items():
            with self.subTest(kind=kind):
                found = record_filter(kind)
                self.assertTrue(found.model.model_fields)
                self.assertLessEqual(found.model.model_fields.keys(), spec.record.model_fields.keys())
                # A record stored without a field is kept by a filter of that field's default.
                for name in found.defaults.keys() - found.listed:
                    asked = found.model.model_validate({name: found.defaults[name]})
                    self.assertTrue(found.keeps({}, asked.model_dump(mode="json", exclude_unset=True)), name)
