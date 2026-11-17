"""Synthetic command declarations for presentation tests."""

from dataclasses import dataclass
from unittest import mock

from hq.platform.application.action_links import command_action
from hq.platform.application.effects import Effect
from hq.platform.application.integration_specs import CapabilitySpec


@dataclass(frozen=True)
class ExampleCommand:
    pass


def example_command(
    name, label, effect, url, *, method="GET", capability=None, target="", reason="", recommended=False, refusal=""
):
    """Derive a command link from the example command's declared words and effect."""

    capability = capability or f"example.{name}"
    spec = CapabilitySpec(
        name=capability,
        summary="An example command.",
        effect=Effect.REMOTE_WRITE if effect == "write" else Effect(effect),
        required_capability="example.run",
        command_type=ExampleCommand,
        handler=lambda **kwargs: {},
        label=label,
        verb=label,
    )
    with (
        mock.patch("hq.platform.application.action_links.capabilities.command_spec", return_value=spec),
        mock.patch("hq.platform.application.action_links.outbound_work.refusal", return_value=refusal),
    ):
        return command_action(
            name, capability, url, target=target, method=method, reason=reason, recommended=recommended
        )
