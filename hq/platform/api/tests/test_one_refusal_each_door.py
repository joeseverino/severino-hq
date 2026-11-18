"""The same malformed narrowing or call gets the same refusal on every door: the
machine API's query string or body, an MCP tool's arguments, the operator's
``hq_call`` and an extension's call answer with one code, one sentence and one
set of details."""

from django.test import TestCase

from hq.platform.application.tests.host_only import host_only

from ..testing import ContractClient
from .doors import API, OPERATOR, At, Caller, at_the_door, each_door
from .tokens import _token

READER = Caller("example-agent", "read")
# A caller that may run every change ``CALLS`` names, so what each door refuses is what the call carried.
WRITER = Caller("example-agent", "read write_projects write_expenses")
FOCUS = "resource:example-focus"
# Calls whose target or stale-write token is not of its kind, each to a capability that takes a target.
CALLS = (
    ("expense.update", {"target": "example-not-digits"}),
    ("project.update", {"target": ""}),
    ("project.update", {"target": "example", "expected_updated_at": "example-not-a-time"}),
    ("project.update", {"target": "example", "expected_updated_at": 1}),
    ("expense.update", {"target": True}),
)
# Each read's tool, the integer it narrows by, and values as an MCP caller
# sends them; the API's query string carries each one's JSON text.
READS = (
    ("get_topology", "depth", ("abc", "2 ", "5_0", "", True, 1.0, 9, -1), {"focus": FOCUS}),
    ("get_findings", "limit", ("many", "+3", "1.0", "", False, 0, -3), {}),
)
# A text narrowing that names none of its choices, on each read.
CHOICES = (
    ("get_topology", {"lens": "example-unknown"}),
    ("get_topology", {"focus": FOCUS, "direction": "sideways"}),
    ("get_findings", {"rule": "example-unknown"}),
    ("get_findings", {"severity": "critical"}),
)


class OneRefusalEachDoorTests(TestCase):
    client_class = ContractClient

    def setUp(self):
        at_the_door(self)
        self.enterContext(host_only())

    def _refused_alike(self, caller, tool, arguments):
        """``arguments`` refused as invalid input, by every door the caller and the operator reach, in one answer."""

        refusal = At(self, API, caller)(tool, **arguments)["error"]
        self.assertEqual(refusal["code"], "invalid_input")
        for asker in (caller, OPERATOR):
            for door in each_door(self, asker, tool):
                with door:
                    answer = door(tool, **arguments)

                    self.assertFalse(answer["ok"])
                    self.assertEqual(answer["error"], refusal)

    def test_an_integer_that_is_not_a_count_is_refused_alike_on_every_door(self):
        for tool, field, values, rest in READS:
            for value in values:
                with self.subTest(tool=tool, value=value):
                    self._refused_alike(READER, tool, {**rest, field: value})

    def test_a_choice_none_declares_is_refused_alike_on_every_door(self):
        for tool, arguments in CHOICES:
            with self.subTest(tool=tool, arguments=arguments):
                self._refused_alike(READER, tool, arguments)

    def test_a_call_carrying_a_target_or_stamp_not_of_its_kind_is_refused_alike_on_every_door(self):
        """Asked of a caller that may run the change, so each door answers for what the call carried."""

        for name, carried in CALLS:
            with self.subTest(name=name, carried=carried):
                self._refused_alike(WRITER, "execute_capability", {"name": name, "payload": {}, **carried})

    def test_a_caller_without_the_grant_is_refused_alike_whatever_it_carried(self):
        """No door tells a reader the shape of target a change it may not run would have taken."""

        for name, carried in CALLS:
            with self.subTest(name=name, carried=carried):
                for door in each_door(self, READER):
                    with door:
                        answer = door.capability(name, {}, **carried)

                        self.assertFalse(answer["ok"])
                        self.assertEqual(answer["error"]["code"], "insufficient_scope")

    def _api(self, query):
        return self.client.get(f"/api/v2/topology/?{query}", HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}")

    def test_a_count_in_digits_reads_as_one(self):
        response = self._api("depth=1")

        self.assertEqual(response.status_code, 200, response.content)

    def test_a_narrowing_sent_twice_is_refused(self):
        for query in ("depth=1&depth=2", "lens=unobserved-resources&lens=unobserved-resources"):
            with self.subTest(query=query):
                response = self._api(query)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_input")
