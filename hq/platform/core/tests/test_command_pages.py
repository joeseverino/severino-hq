"""Every command's page says what it changes, where it stands and what to press.

Walked over the whole catalog, extensions' commands included when they are
installed, so a command declared tomorrow is held to it without a test of its
own: its effect stated, its buttons in its own words (a switch's as the two
words that set it, or its checkbox and name where it declares none), no warning of a blank it cannot save, and, for one an
agent may ask for, a held request said as what it wants done to its target.
"""

from dataclasses import replace
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, DashboardMachine, ManagedResource
from hq.domains.projects.models import Project
from hq.platform.application.action_links import command_url
from hq.platform.application.agent_access import SET_AGENT_ACCESS
from hq.platform.application.approvals import asked
from hq.platform.application.attention import waiting_for_approval
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.capability_policy import agents_may_hold
from hq.platform.application.glance import DASHBOARD_MACHINE
from hq.platform.application.integration_specs import CapabilitySpec, command_schema
from hq.platform.application.integrations import integration_graph, override_integration_graph
from hq.platform.application.plugins import plugin_capability_specs
from hq.platform.application.projects import DEPENDENCY_UPDATES
from hq.platform.application.security import Capability
from hq.platform.application.tests.markup import select, with_class, within
from hq.platform.core.models import AgentAccess
from hq_sdk.capabilities import StrictCommand

from .command_shapes import SWITCH_WORDS, optional_fields, sets_yes_or_no

User = get_user_model()
# How each kind of change is said on its page: a note, or the confirmation
# its button waits on. A read says nothing.
NOTED = {"remote_write"}
CONFIRMED = {"infrastructure_change", "destructive"}


def buttons(page) -> list[str]:
    return [element.words for element in within(page, "data-command-form") if element.attrs.get("type") == "submit"]


def stated(page, effect: str) -> bool:
    if effect in CONFIRMED:
        return bool(select(page, "name", "__confirm_effect"))
    return effect not in NOTED or bool(with_class(page, "command-effect-note"))


def switch_words(spec) -> list[str]:
    declared = dict(spec.variants)
    return [declared.get(variant, "") for variant in SWITCH_WORDS]


def pressed_as_words(spec) -> bool:
    """A switch that declares both words, which it is pressed by."""

    return sets_yes_or_no(command_schema(spec.command_type)) and all(switch_words(spec))


def checkboxes(page) -> list[str]:
    return [
        element.attrs.get("name", "")
        for element in within(page, "data-command-form")
        if element.tag == "input" and element.attrs.get("type") == "checkbox"
    ]


class EveryCommandPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("example-operator", "operator@example.test", "x" * 20)

    def setUp(self):
        self.client.force_login(self.user)

    def page(self, name: str, target: str = ""):
        return self.client.get(command_url(name, target))

    def test_every_page_states_its_effect_and_says_its_commands_words(self):
        for spec in capability_registry().values():
            with self.subTest(command=spec.name):
                page = self.page(spec.name)

                self.assertEqual(page.status_code, 200)
                self.assertTrue(stated(page, spec.effect), f"{spec.effect} is not said on the page")
                if pressed_as_words(spec):
                    # A switch is pressed as what it sets, never as its name;
                    # one that knows where it stands offers only the change.
                    self.assertTrue(buttons(page), "no button")
                    self.assertLessEqual(set(buttons(page)), set(switch_words(spec)))
                else:
                    self.assertEqual(buttons(page), [spec.title])

    def test_every_switch_the_host_registers_declares_its_words(self):
        # An extension adopts the words in its own time, and keeps its
        # checkbox until then (WordlessSwitchTests).
        extensions = {spec.name for spec in plugin_capability_specs()}
        switches = [
            spec
            for spec in capability_registry().values()
            if spec.name not in extensions and sets_yes_or_no(command_schema(spec.command_type))
        ]
        for spec in switches:
            with self.subTest(command=spec.name):
                self.assertTrue(all(switch_words(spec)), "a switch declares its on and off words")
        self.assertIn(DEPENDENCY_UPDATES, {spec.name for spec in switches})

    def test_only_a_form_that_can_blank_a_field_warns_of_it(self):
        warned = set()
        for spec in capability_registry().values():
            schema = command_schema(spec.command_type)
            with self.subTest(command=spec.name):
                if not select(self.page(spec.name), "data-command-blanks"):
                    continue
                warned.add(spec.name)
                # It acts on a record, opens without its values, and takes a
                # field it may be sent without.
                self.assertTrue(spec.target_kind)
                self.assertFalse(spec.target_initial_fields)
                self.assertTrue(optional_fields(schema))
        # The warning is still drawn where it is true.
        self.assertIn("project.update", warned)


class EveryHeldRequestTests(TestCase):
    """What a person reads when an agent's call waits for them: on the card
    and in the bell, the agent wants to do something, to its target."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("example-operator", "operator@example.test", "x" * 20)

    def setUp(self):
        self.client.force_login(self.user)

    def said(self, held) -> tuple[str, str]:
        page = self.client.get(reverse("core:approval_entry", kwargs={"approval_id": held.id}), follow=True)
        (card,) = [element.words for element in within(page, "class", "approval-head") if element.tag == "h2"]
        (bell,) = [item.title for item in waiting_for_approval() if item.key == f"approval:{held.id}"]
        return card, bell

    def test_every_command_an_agent_may_ask_for_says_what_it_wants_done(self):
        asked = [spec for spec in capability_registry().values() if spec.effect != "read" and agents_may_hold(spec)]
        for spec in asked:
            target = ("7" if spec.target_kind == "integer" else "example-target") if spec.target_kind else ""
            held = ApprovalRequest.objects.create(
                capability=spec.name,
                target=target,
                requested_actor="example-agent",
                requested_interface="mcp",
                content_fingerprint="0" * 64,
                expires_at=timezone.now() + timedelta(hours=1),
            )
            with self.subTest(command=spec.name):
                for said in self.said(held):
                    words = said.split()
                    self.assertEqual(words[:3], ["example-agent", "wants", "to"], said)
                    self.assertNotIn("run", words, said)
                    if target:
                        self.assertIn(target, words, said)
            held.delete()
        # The commands an agent may ask for are walked.
        self.assertIn("infrastructure.resource.update", {spec.name for spec in asked})


class SwitchPageTests(TestCase):
    """The dependency update switch, as opened from a project."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("example-operator", "operator@example.test", "x" * 20)
        for slug, on in (("example-on", True), ("example-off", False)):
            Project.objects.create(
                name=slug, slug=slug, repository_url=f"https://github.com/example/{slug}", dependency_updates=on
            )

    def setUp(self):
        self.client.force_login(self.user)

    def test_it_says_where_the_project_stands_and_offers_only_the_change(self):
        spec = capability_registry()[DEPENDENCY_UPDATES]
        for slug, state, offered in (
            ("example-on", "On", spec.words("off")),
            ("example-off", "Off", spec.words("on")),
        ):
            with self.subTest(project=slug):
                page = self.client.get(command_url(DEPENDENCY_UPDATES, slug))

                said = [element.words for element in within(page, "data-command-state") if element.tag == "strong"]
                self.assertEqual(said, [state])
                self.assertEqual(buttons(page), [offered])
                self.assertFalse(select(page, "data-command-blanks"))

    def test_pressing_the_change_sets_it(self):
        page = self.client.get(command_url(DEPENDENCY_UPDATES, "example-on"))
        form = page.context["form"]

        response = self.client.post(
            command_url(DEPENDENCY_UPDATES),
            {
                "__target": "example-on",
                "__execution_key": form.initial["__execution_key"],
                "__expected_updated_at": form.initial["__expected_updated_at"],
                "dependency_updates": "false",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Project.objects.get(slug="example-on").dependency_updates)


class DeclaredStateSwitchTests(TestCase):
    """A switch whose state no record's detail holds says where it stands
    from its command's own ``state``: the agents' pause, which takes no
    record, and a machine's place on the dashboard, which takes one."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("example-operator", "operator@example.test", "x" * 20)

    def setUp(self):
        self.client.force_login(self.user)

    def said(self, page) -> list[str]:
        return [element.words for element in within(page, "data-command-state") if element.tag == "strong"]

    def test_the_agents_pause_says_where_it_stands(self):
        spec = capability_registry()[SET_AGENT_ACCESS]
        for paused, state, offered in ((True, "On", spec.words("off")), (False, "Off", spec.words("on"))):
            AgentAccess.objects.update_or_create(pk=1, defaults={"paused": paused})
            with self.subTest(paused=paused):
                page = self.client.get(command_url(SET_AGENT_ACCESS))

                self.assertEqual(self.said(page), [state])
                self.assertEqual(buttons(page), [offered])

    def test_a_machines_place_on_the_dashboard_says_where_it_stands(self):
        spec = capability_registry()[DASHBOARD_MACHINE]
        shown, hidden = (
            ManagedResource.objects.create(key=key, kind="machine", spec={"name": key}, enabled=True)
            for key in ("example-shown", "example-hidden")
        )
        DashboardMachine.objects.create(machine=shown)
        for machine, state, offered in ((shown, "On", spec.words("off")), (hidden, "Off", spec.words("on"))):
            with self.subTest(machine=machine.key):
                page = self.client.get(command_url(DASHBOARD_MACHINE, machine.key))

                self.assertEqual(self.said(page), [state])
                self.assertEqual(buttons(page), [offered])


class RemindersCommand(StrictCommand):
    reminders: bool = False


SENT: list[bool] = []


def set_reminders(command, *, principal):
    SENT.append(command.reminders)
    return {"ok": True}


# A switch an extension declares before it adopts the on and off words.
WORDLESS = CapabilitySpec(
    "example.reminders.set",
    "Send or hold the example reminders.",
    "remote_write",
    Capability.READ,
    RemindersCommand,
    set_reminders,
    label="Send reminders",
)


class WordlessSwitchTests(TestCase):
    """A switch without its words is still a working command: its checkbox,
    pressed by its name, and a held request said by that name."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("example-operator", "operator@example.test", "x" * 20)

    def setUp(self):
        self.client.force_login(self.user)
        graph = integration_graph()
        composed = replace(graph, capabilities={**graph.capabilities, WORDLESS.name: WORDLESS})
        self.enterContext(override_integration_graph(composed))
        SENT.clear()

    def test_it_is_a_checkbox_pressed_by_its_name(self):
        page = self.client.get(command_url(WORDLESS.name))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(checkboxes(page), ["reminders"])
        self.assertEqual(buttons(page), [WORDLESS.title])

    def test_it_sets_what_the_checkbox_holds(self):
        for posted, value in (({"reminders": "on"}, True), ({}, False)):
            with self.subTest(value=value):
                form = self.client.get(command_url(WORDLESS.name)).context["form"]
                response = self.client.post(
                    command_url(WORDLESS.name),
                    {"__execution_key": form.initial["__execution_key"], **posted},
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(SENT[-1], value)

    def test_a_held_request_says_it_by_its_name(self):
        for value in (True, False):
            with self.subTest(value=value):
                held = ApprovalRequest(
                    capability=WORDLESS.name, payload={"reminders": value}, requested_actor="example-agent"
                )

                self.assertEqual(str(asked(held)), "example-agent wants to send reminders")
