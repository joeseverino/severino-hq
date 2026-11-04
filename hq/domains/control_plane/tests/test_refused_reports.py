"""A controller report the bridge refuses is a finding until the same report is accepted."""

import json

from django.apps import apps
from django.test import TestCase

from hq.platform.application.findings import findings
from hq.platform.application.security import Capability, Principal

from . import bridge_client

EVERYTHING = Principal("test", "operator", frozenset(Capability))
# A member the contract types as a boolean, sent as a string.
DEPARTING = {"adguard.rewrite": {"ok": "false", "records": []}}
POINTER = "/adguard.rewrite/ok"
CONFORMING = {"adguard.rewrite": {"ok": True, "records": []}}


def kept():
    """The rows the bridge keeps, by name, so this module imports at a ref without them."""

    return apps.get_model("control_plane", "RefusedReport").objects


def refused() -> list[dict]:
    return findings(principal=EVERYTHING, rule="report-refused")["findings"]


def send(payload: dict, controller: str = "example-controller") -> int:
    return bridge_client.request(
        "/inventory", query={"controller-id": controller}, body=json.dumps(payload).encode()
    ).status


class RefusedReportTests(TestCase):
    def test_a_refused_inventory_is_one_finding_until_one_is_accepted(self):
        self.assertEqual(send(DEPARTING), 422)

        (finding,) = refused()

        said = " ".join([finding["title"], *(item["value"] for item in finding["evidence"])])
        self.assertIn("inventory", said)
        self.assertIn(POINTER, said)
        self.assertEqual(finding["severity"], "serious")

        self.assertEqual(send(CONFORMING), 200)

        self.assertEqual(refused(), [])

    def test_a_refusal_repeated_every_pass_stays_one(self):
        for _ in range(3):
            send(DEPARTING)

        self.assertEqual(kept().count(), 1)
        self.assertEqual(len(refused()), 1)

    def test_another_controllers_accepted_report_leaves_it_open(self):
        send(DEPARTING)
        send(CONFORMING, controller="other-controller")

        self.assertEqual(len(refused()), 1)

    def test_a_refusal_keeps_where_and_never_what_was_sent(self):
        self.assertEqual(send({"adguard.rewrite": {"ok": True, "records": ["app.example.com"]}}), 422)

        (row,) = kept().all()
        self.assertIn("/adguard.rewrite/records/0", row.detail)
        self.assertNotIn("app.example.com", row.detail)

    def test_a_query_holding_a_secret_names_no_controller(self):
        token = "ghp_" + "a" * 36
        answer = bridge_client.request("/inventory", query={"controller-id": token}, body=b"{}")

        self.assertEqual(answer.status, 422)
        (row,) = kept().all()
        self.assertEqual(row.controller_id, "")
        self.assertNotIn(token, row.detail)
