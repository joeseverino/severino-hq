"""A capability call's envelope is read by the schema the document publishes for it:
what the schema refuses, the API refuses, before any capability runs, naming
no value the request carried."""

import json

from django.test import TestCase, override_settings
from jsonschema import Draft202012Validator

from hq.platform.application.call_envelope import ENVELOPE
from hq.platform.application.capabilities import describe_capabilities
from hq.platform.application.security import cli_principal

from .. import views
from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

SECRET = "example-secret-value"


def _schema(name):
    spec = next(
        spec for spec in describe_capabilities(principal=cli_principal())["capabilities"] if spec["name"] == name
    )
    return views.request_schema(spec)


def _envelope(name):
    """``name``'s request schema, any object taken as its command: what the envelope itself allows."""

    schema = _schema(name)
    return Draft202012Validator({**schema, "properties": {**schema["properties"], "payload": {"type": "object"}}})


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
)
class CallEnvelopeTests(TestCase):
    client_class = ContractClient

    def _post(self, name, body, key):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps(body),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects write_expenses')}",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def test_what_the_schema_refuses_the_api_refuses(self):
        cases = {
            "expense.update": [
                {"payload": {}, "target": f"{SECRET}"},
                {"payload": {}, "target": True},
            ],
            "project.update": [
                {"payload": {}, "target": ""},
                {"payload": {}, "target": None},
                {"payload": [], "target": "example"},
                {"payload": {}, "target": "example", "expected_updated_at": 1},
                {"payload": {}, "target": "example", "example_unknown": 1},
            ],
        }
        for name, bodies in cases.items():
            validator = _envelope(name)
            for attempt, body in enumerate(bodies):
                with self.subTest(name=name, body=body):
                    self.assertFalse(validator.is_valid(body))
                    response = self._post(name, body, f"envelope-{name}-{attempt}")
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertEqual(response.json()["error"]["code"], "invalid_input")
                    self.assertNotIn(SECRET, response.content.decode())

    def test_a_stale_write_token_that_is_no_time_is_refused(self):
        """The schema's ``format`` is an annotation a validator may skip; the API holds to it."""

        response = self._post(
            "project.update", {"payload": {}, "target": "example", "expected_updated_at": SECRET}, "envelope-time"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")
        self.assertNotIn(SECRET, response.content.decode())

    def test_an_integer_target_may_be_its_digits(self):
        validator = _envelope("expense.update")
        for target in (7, "7", "-7"):
            with self.subTest(target=target):
                self.assertTrue(validator.is_valid({"payload": {}, "target": target}))
                self.assertEqual(
                    ENVELOPE.validate_python({"target": target}, context={"target_kind": "integer"})["target"],
                    target,
                )

    def test_a_time_as_written_reaches_the_capability(self):
        written = "2026-11-02T14:30:00.123456+00:00"
        self.assertEqual(ENVELOPE.validate_python({"expected_updated_at": written})["expected_updated_at"], written)

    def test_a_command_with_required_fields_requires_its_payload(self):
        self.assertIn("payload", _schema("expense.create")["required"])
        # An update requires only its target.
        self.assertEqual(_schema("expense.update")["required"], ["target"])
        self.assertNotIn(
            "payload", views.request_schema({"input_schema": {"type": "object"}, "target": None})["required"]
        )
