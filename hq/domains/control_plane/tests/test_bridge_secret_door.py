"""The bridge takes in no secret: a credential anywhere in a payload is struck at its door."""

import json
from dataclasses import replace
from typing import Any

from django.test import TestCase
from pydantic import BaseModel

from hq.domains.control_plane import bridge_application
from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.domains.control_plane.bridge_contract import operations

from . import bridge_client

# What stands where a secret was.
REDACTED = "[redacted]"


# One payload each action takes, conforming to the contract, with a string in
# every place a provider's words can arrive.
EXAMPLES: dict[str, Any] = {
    "report": {
        "success": True,
        "observed_generation": 1,
        "status": {"note": "Applied.", "consumers": [{"consumer": "example-proxy"}]},
        "conditions": [{"type": "Ready", "status": True, "reason": "Applied", "message": "Applied."}],
        "message": "Applied.",
        "events": [{"host": "app.example.com", "task": "Restart", "outcome": "changed", "message": "Restarted."}],
    },
    "connections": [
        {
            "connection_ref": "example-npm",
            "provider": "npm",
            "endpoint": "https://proxy.example.com",
            "manages": True,
            "probed": True,
            "ok": True,
            "detail": "Authenticated.",
            "reaches": ["proxy.example.com"],
        }
    ],
    "inventory": {
        "adguard.rewrite": {
            "ok": True,
            "records": [{"domain": "app.example.com", "answer": "192.0.2.10", "enabled": True}],
        }
    },
    "steps": [{"subject": "example-npm", "step": "npm.proxy_host:reconcile", "reason": "refused"}],
    "analytics-plan": [{"site_tag": "tag", "connection_ref": "example-api"}],
    "analytics": {
        "sites": [
            {
                "site_tag": "tag",
                "host": "example.com",
                "connection_ref": "example-api",
                "start": "2026-08-12",
                "end": "2026-08-12",
                "rows": [
                    {
                        "dimension": "path",
                        "value": "/about/",
                        "date": "2026-08-12",
                        "pageviews": 3,
                        "visits": 2,
                        "sample_interval": 1,
                    }
                ],
                "vitals": [],
            }
        ]
    },
    "glance": [{"panel": "example", "as_of": "2026-08-12T00:00:00+00:00", "summary": "Fine."}],
}

# A synthetic of each credential the controller holds or mints, assembled here
# so the source holds none whole.
SECRETS = {
    "a private key": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBexample",
    "an OpenSSH private key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaA",
    **{f"a GitHub {prefix} token": prefix + "Ex4mple" * 6 for prefix in ("ghp_", "gho_", "ghs_", "ghu_", "ghr_")},
    "a fine-grained GitHub token": "github_" + "pat_11EXAMPLE0" + "a" * 60,
    "a Tailscale key": "tskey-" + "api-kEXAMPLE1CNTRL-" + "e" * 30,
    "a Cloudflare token": "cfut_" + "E" * 40 + "0a1b2c3d",
    "a 1Password service account token": "ops_" + "eyJ" + "e" * 60,
    "a Portainer access token": "ptr_" + "E" * 43 + "=",
    "an age identity": "AGE-SECRET-" + "KEY-1" + "Q" * 58,
    "a JSON Web Token": "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiJleGFtcGxlIn0" + ".c2lnbmF0dXJlLWV4YW1wbGU",
}


def _strings(value: Any, path: tuple[str | int, ...] = ()):
    """Every string leaf of ``value``, by its path."""

    if isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _strings(child, (*path, index))
    elif isinstance(value, str):
        yield path


def _planted(value: Any, path: tuple[str | int, ...], text: str) -> Any:
    copy = json.loads(json.dumps(value))
    node = copy
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = f"{node[path[-1]]} {text} rejected"
    return copy


def _pointer(path: tuple[str | int, ...]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _recording(ran: list[tuple[str, Any]]):
    """The bridge with each action replaced by a note of the payload it was given."""

    def noted(name: str):
        return lambda parameters, payload: ran.append((name, payload)) or {"ok": True}

    return bridge_application.bridge(tuple(replace(action, run=noted(action.name)) for action in ACTIONS))


def _at(value: Any, path: tuple[str | int, ...]) -> Any:
    for part in path:
        value = value[part]
    return value


def _query(action: str) -> dict[str, str]:
    names = {parameter.name for parameter in operations()[action].parameters}
    return {name: value for name, value in (("controller-id", "example"), ("operation", "1")) if name in names}


def _send(action: str, payload: Any, app: Any) -> bridge_client.Answer:
    return bridge_client.request(f"/{action}", query=_query(action), body=json.dumps(payload).encode(), app=app)


class InboundDoorTests(TestCase):
    def test_every_action_with_a_payload_has_an_example_that_conforms_and_passes(self):
        taking = {name for name, operation in operations().items() if operation.takes_body}
        self.assertEqual(set(EXAMPLES), taking)
        ran: list[tuple[str, Any]] = []
        app = _recording(ran)
        for action, payload in EXAMPLES.items():
            with self.subTest(action=action):
                self.assertIsNone(operations()[action].violation(payload))
                answer = _send(action, payload, app)
                self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(ran, list(EXAMPLES.items()))

    def test_a_secret_in_any_string_of_any_payload_is_struck_where_it_stands(self):
        # The actions where a planted string still conforms, so the action ran
        # on what the door struck.
        reached: set[str] = set()
        for action, payload in EXAMPLES.items():
            for path in _strings(payload):
                for shape, secret in SECRETS.items():
                    planted = _planted(payload, path, secret)
                    ran: list[tuple[str, Any]] = []
                    with (
                        self.subTest(action=action, at=_pointer(path), shape=shape),
                        self.assertLogs("severino.bridge", "WARNING") as logged,
                    ):
                        answer = _send(action, planted, _recording(ran))
                        self.assertNotIn(secret, answer.body.decode())
                        self.assertNotIn(secret, "\n".join(logged.output))
                        self.assertIn(_pointer(path), "\n".join(logged.output))
                        if operations()[action].violation(planted) is not None:
                            # A member the controller fills from a closed list.
                            self.assertEqual(answer.status, 422, answer.body)
                            continue
                        reached.add(action)
                        self.assertEqual(answer.status, 200, answer.body)
                        [(name, given)] = ran
                        self.assertEqual(name, action)
                        self.assertNotIn(secret, json.dumps(given))
                        self.assertIn(REDACTED, _at(given, path))
                        self.assertIsNone(operations()[action].violation(given))
        self.assertEqual(reached, set(EXAMPLES))

    def test_a_secret_in_a_query_parameter_is_refused(self):
        ran: list[tuple[str, Any]] = []
        answer = bridge_client.request(
            "/claim", query={"controller-id": SECRETS["a GitHub ghs_ token"]}, app=_recording(ran)
        )
        self.assertEqual(answer.status, 422, answer.body)
        self.assertIn("/controller-id", answer.json()["detail"])
        self.assertEqual(ran, [])

    def test_a_member_the_contract_leaves_open_has_its_strings_struck_by_a_secret_name(self):
        # Struck in place, so a reader of the record finds the shape it was sent.
        cases = (
            ("must-never-enter-hq", REDACTED),
            ({"value": "x", "count": 7}, {"value": REDACTED, "count": 7}),
            (["x", None, True], [REDACTED, None, True]),
        )
        for key in ("api_token", "Password", "client_secret", "private_key", "credentials"):
            for value, kept in cases:
                ran: list[tuple[str, Any]] = []
                report = {**EXAMPLES["report"], "status": {"nested": {key: value}}}
                with self.subTest(key=key, value=value), self.assertLogs("severino.bridge", "WARNING"):
                    answer = _send("report", report, _recording(ran))
                    self.assertEqual(answer.status, 200, answer.body)
                    self.assertEqual(ran[0][1]["status"], {"nested": {key: kept}})

    def test_a_flag_named_like_a_secret_holds_none_and_passes(self):
        # A repository's ``private`` is the contract's own member.
        ran: list[tuple[str, Any]] = []
        status = {"private": True, "token_expires_at": None}
        answer = _send("report", {**EXAMPLES["report"], "status": status}, _recording(ran))
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(ran[0][1]["status"], status)

    def test_words_that_only_mention_a_credential_pass_unchanged(self):
        ran: list[tuple[str, Any]] = []
        said = "The token was refused; pass a tskey- or ghp_ value, and the PRIVATE KEY stays in 1Password."
        answer = _send("steps", [{**EXAMPLES["steps"][0], "reason": said}], _recording(ran))
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(ran[0][1][0]["reason"], said)


def _submission(message: str) -> dict[str, Any]:
    """One contact form row as the controller reads it from D1: a visitor wrote its words."""

    return {
        "connection_ref": "example-api",
        "account_id": "example-account",
        "database_id": "example-database",
        "database": "contact",
        "id": 7,
        "created_at": "2026-08-12T10:00:00Z",
        "updated_at": "2026-08-12T10:00:00Z",
        "name": "A visitor",
        "email": "visitor@example.com",
        "message": message,
        "status": "new",
        "user_agent": "Mozilla/5.0",
        "source_url": "https://example.com/contact/",
    }


class OutsiderTextTests(TestCase):
    """What a stranger typed reaches HQ through an inventory, and cannot stop it."""

    def test_a_contact_submission_holding_a_secret_lands_with_it_struck(self):
        from hq.domains.control_plane.models import ProviderInventory

        for shape, secret in SECRETS.items():
            with self.subTest(shape=shape), self.assertLogs("severino.bridge", "WARNING"):
                inventory = {
                    "cloudflare.contact_submission": {"ok": True, "records": [_submission(f"Hi, {secret} rejected")]},
                    "adguard.rewrite": EXAMPLES["inventory"]["adguard.rewrite"],
                }
                bridge_client.call("inventory", inventory, controller_id="example")

                stored = ProviderInventory.objects.get(kind="cloudflare.contact_submission").records
                self.assertEqual(len(stored), 1)
                self.assertNotIn(secret, json.dumps(stored))
                self.assertTrue(stored[0]["message"].startswith(f"Hi, {REDACTED}"))
                self.assertEqual(len(ProviderInventory.objects.get(kind="adguard.rewrite").records), 1)


class AnswerTests(TestCase):
    def _app(self, run) -> Any:
        return bridge_application.bridge(
            tuple(replace(action, run=run) if action.name == "steps" else action for action in ACTIONS)
        )

    def test_an_echo_of_the_payload_cannot_carry_a_secret_past_the_door(self):
        # What an action's own refusal repeats is what the controller sent,
        # which the door has already struck.
        def echo(parameters, payload):
            raise ValueError(f"No step named {payload[0]['step']}.")

        app = self._app(echo)
        said = _send("steps", [{**EXAMPLES["steps"][0], "step": "example-step"}], app)
        self.assertEqual(said.status, 400, said.body)
        self.assertIn("example-step", said.json()["detail"])

        secret = SECRETS["a GitHub ghs_ token"]
        with self.assertLogs("severino.bridge", "WARNING"):
            echoed = _send("steps", [{**EXAMPLES["steps"][0], "step": secret}], app)
        self.assertEqual(echoed.status, 400, echoed.body)
        self.assertNotIn(secret, echoed.body.decode())
        self.assertIn(REDACTED, echoed.json()["detail"])

    def test_a_validation_error_says_where_and_never_the_input(self):
        class Step(BaseModel):
            attempts: int

        def validated(parameters, payload):
            Step.model_validate({"attempts": payload[0]["reason"]})

        answer = _send("steps", [{**EXAMPLES["steps"][0], "reason": "example-unparsed-words"}], self._app(validated))
        self.assertEqual(answer.status, 400, answer.body)
        detail = answer.json()["detail"]
        self.assertIn("attempts", detail)
        self.assertNotIn("example-unparsed-words", detail)
