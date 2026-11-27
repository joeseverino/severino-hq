"""A procedure's run is asked on its page through a form drawn from its
command: the machines it may run on to choose from, and its own inputs as
fields. Every manifest here is a synthetic ``example.*`` one."""

from typing import Annotated
from unittest.mock import patch

from django.test import TestCase
from pydantic import Field

from hq.platform.application.contracts import Closed

from ..capabilities import command_spec, run_capability
from ..command_forms import command_form_class
from ..command_targets import record_options
from ..integration_specs import capability_schema
from ..security import Capability, Principal
from .procedures import APP, RESTART_RUN, Supply, manifest

# The box a change's form is posted with ticked.
CONFIRMED = {"__confirm_effect": "on"}
AGENT = Principal("example-agent", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))


class Window(Closed):
    start: Annotated[str, Field(min_length=1, description="When it may start.")]
    hours: Annotated[int, Field(ge=1, le=12, description="How long it may take.")] = 2


class Maintenance(Closed):
    service: Annotated[str, Field(min_length=1, description="The service to look after.")]
    window: Window


class RunFormTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())
        # The machines the machine list reads, as HQ's facts would report them.
        machines = patch(
            "hq.platform.application.machines.machine_catalog", side_effect=lambda **_: self.supply.machines
        )
        machines.start()
        self.addCleanup(machines.stop)

    def form_class(self):
        spec = command_spec(RESTART_RUN)
        return command_form_class(spec, record_choices=record_options(capability_schema(spec), principal=AGENT))

    def asked(self, form):
        """What the command answers the payload the form posts, as the page sends it."""

        return run_capability(
            RESTART_RUN, form.command_payload, principal=AGENT, idempotency_key="example-form-run"
        ).answer

    def offered(self):
        return sorted(value for value, _label in self.form_class().base_fields["hosts"].choices)

    def test_a_procedure_written_for_no_machine_offers_none(self):
        self.supply(manifest(target_kinds=("example.appliance",)))

        self.assertEqual(self.offered(), [])

    def test_an_object_inside_the_inputs_is_drawn_as_its_fields_too(self):
        self.supply(manifest(input_type=Maintenance))
        form_class = self.form_class()

        self.assertTrue(form_class.base_fields["parameters.window.start"].required)
        self.assertFalse(form_class.base_fields["parameters.window.hours"].required)
        form = form_class(
            {"hosts": [APP], "parameters.service": "web", "parameters.window.start": "02:00", **CONFIRMED}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.command_payload["parameters"], {"service": "web", "window": {"start": "02:00"}})
        self.assertTrue(self.asked(form)["ok"])
