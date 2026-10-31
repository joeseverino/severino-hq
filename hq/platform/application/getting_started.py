"""What a new HQ still needs, in the order it is worth doing.

Derived, never stored: each step is done when HQ can see it is, so there is
nothing to tick off or forget to untick, and an HQ with every step done shows
none. A first project is not a step, since the dashboard's Projects card
already asks for it.
"""

from dataclasses import dataclass

from .projection import read_once
from .routes import reverse
lazy from . import cadence
lazy from .connections import connection_rows
lazy from .glance import dashboard_machines


@dataclass(frozen=True, slots=True)
class SetupStep:
    id: str
    label: str
    why: str
    url: str
    done: bool
    # The same icon its tile on the dashboard carries.
    icon: str = ""


def steps() -> tuple[SetupStep, ...]:
    """Every step, done or not; ``remaining`` is what a page shows."""

    return read_once("getting_started.steps", _steps)


def remaining() -> tuple[SetupStep, ...]:
    return tuple(step for step in steps() if not step.done)


def _steps() -> tuple[SetupStep, ...]:
    return (
        SetupStep(
            "controller",
            "Start the controller",
            "It reads your machines and services and applies the changes HQ asks for.",
            reverse("control_plane:controller"),
            cadence.controller_standing().known,
            icon="activity",
        ),
        SetupStep(
            "connection",
            "Add a connection",
            "Cloudflare, Tailscale, Portainer or your proxy: whatever HQ should read.",
            reverse("control_plane:connections"),
            bool(connection_rows()),
            icon="link",
        ),
        SetupStep(
            "machine",
            "Choose a machine for the dashboard",
            "Its load and containers then show at the top of this page.",
            reverse("control_plane:machines"),
            bool(dashboard_machines()),
            icon="server",
        ),
    )
