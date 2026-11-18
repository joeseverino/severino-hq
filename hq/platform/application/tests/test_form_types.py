"""A form's text reaches its command in the command's types, and only a form's.

A form posts text (a weekday's checkbox sends ``"0"``), where an agent's
payload is held to the JSON types its schema publishes.
"""

from dataclasses import dataclass
from typing import Annotated

from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from pydantic import Field

from ..capabilities import CapabilitySpec, execute_capability
from ..integration_testing import commands_registered
from ..security import Capability, cli_principal
from ..web_writes import RETRY_KEY_FIELD, run_posted

Weekday = Annotated[int, Field(ge=0, le=6)]


@dataclass(frozen=True, slots=True)
class PlanCommand:
    days: list[Weekday] | None = None
    split: list[tuple[Weekday, str]] | None = None
    sessions: int | None = None


# What a plan's form posts: each number as its text, as a choice sends it.
AS_POSTED = {"days": ["0", "2"], "split": [["1", "chest"]], "sessions": "5"}


class FormTypeTests(TestCase):
    def setUp(self):
        self.ran = []
        self.spec = CapabilitySpec(
            "example.plan.update",
            "Change the example plan.",
            "remote_write",
            Capability.READ,
            PlanCommand,
            self.handler,
        )
        self.person = get_user_model().objects.create_superuser("operator", password="unused-test-pass")

    def handler(self, command, **_bound):
        self.ran.append(command)
        return {"ok": True}

    def posted(self, payload, key):
        request = RequestFactory().post("/example/plan/", {RETRY_KEY_FIELD: key})
        request.user = self.person
        SessionMiddleware(lambda request: HttpResponse()).process_request(request)
        with commands_registered({self.spec.name: self.spec}):
            return run_posted(request, self.spec.name, payload).answer

    def test_a_forms_text_runs_its_command_in_the_commands_types(self):
        answer = self.posted(AS_POSTED, "example-plan-text")

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.ran, [PlanCommand(days=[0, 2], split=[(1, "chest")], sessions=5)])

    def test_a_forms_value_its_type_refuses_runs_nothing(self):
        answer = self.posted({**AS_POSTED, "days": ["9"]}, "example-plan-past")

        self.assertEqual(answer["error"]["code"], "invalid_input")
        self.assertEqual(self.ran, [])

    def test_an_agents_text_for_a_number_is_still_refused(self):
        with commands_registered({self.spec.name: self.spec}):
            answer = execute_capability(self.spec.name, AS_POSTED, principal=cli_principal())

        self.assertEqual(answer["error"]["code"], "invalid_input")
        self.assertEqual(self.ran, [])
