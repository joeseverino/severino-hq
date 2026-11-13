"""The guards that walk every command walk each procedure's run too.

A run's command exists only for a procedure a person adopted, which no
host test does, so each guard's walk is run here with three synthetic
procedures supplied, one of each effect. Where a guard walks only the
registry, it walks the runs alone; its tests that name a host command are
left to the guard itself.
"""

from unittest.mock import patch

from hq.platform.api.tests import test_unkeyed_writes
from hq.platform.core.tests import test_command_pages

from . import test_capability_verbs, test_command_descriptions, test_command_forms
from .procedures import Supply, manifest

PROCEDURES = (
    manifest(),
    manifest(id="example.service_logs", effect="read", retry="safe"),
    manifest(id="example.disk_wipe", effect="destructive", retry="never"),
)


def _runs_only(module):
    whole = module.capability_registry
    return patch.object(
        module,
        "capability_registry",
        lambda: {name: spec for name, spec in whole().items() if name.startswith("automation.")},
    )


def walked(guard: type, module, *walks: str, runs_only: bool = True) -> type:
    """``guard`` with only its ``walks``, over a registry the procedures are supplied to."""

    def setUp(self):
        Supply(self, *PROCEDURES)
        if runs_only:
            patcher = _runs_only(module)
            patcher.start()
            self.addCleanup(patcher.stop)
        guard.setUp(self)

    # A name bound to None is no test, so the guard's other tests stay its own.
    left = {name: None for name in dir(guard) if name.startswith("test_") and name not in walks}
    return type(f"Run{guard.__name__}", (guard,), {**left, "setUp": setUp, "__module__": __name__})


RunCapabilityVerbTests = walked(
    test_capability_verbs.CapabilityVerbTests,
    test_capability_verbs,
    "test_a_capability_is_named_starting_with_its_button_s_word",
)
RunCommandDescriptionTests = walked(
    test_command_descriptions.CommandDescriptionTests,
    test_command_descriptions,
    "test_a_form_shows_each_description_s_meaning_as_its_help",
    "test_every_host_command_field_is_described",
)
RunCapabilityCommandFormTests = walked(
    test_command_forms.CapabilityCommandFormTests,
    test_command_forms,
    "test_no_form_help_says_what_only_a_client_reads",
)
RunUnkeyedWriteTests = walked(
    test_unkeyed_writes.UnkeyedWriteTests,
    test_unkeyed_writes,
    "test_every_change_through_the_api_without_its_header_is_refused",
    "test_every_change_through_mcp_without_a_key_is_refused",
)
RunEveryCommandPageTests = walked(
    test_command_pages.EveryCommandPageTests,
    test_command_pages,
    "test_every_page_states_its_effect_and_says_its_commands_words",
    "test_only_a_form_that_can_blank_a_field_warns_of_it",
)
# Its walk asks that a host command is among those walked, so it walks them all.
RunEveryHeldRequestTests = walked(
    test_command_pages.EveryHeldRequestTests,
    test_command_pages,
    "test_every_command_an_agent_may_ask_for_says_what_it_wants_done",
    runs_only=False,
)
