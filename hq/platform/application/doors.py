"""How each interface a principal comes through is named: alone, and in a sentence beside who came through it."""

from .security import (
    API_INTERFACE,
    CLI_INTERFACE,
    CONTROLLER_INTERFACE,
    DEVICE_INTERFACE,
    INTERNAL_INTERFACE,
    JOB_INTERFACE,
    MCP_INTERFACE,
    WEB_INTERFACE,
)

# Each interface named on its own, as in a list of them: "MCP, API".
LABELS = {
    WEB_INTERFACE: "web",
    DEVICE_INTERFACE: "device",
    CLI_INTERFACE: "CLI",
    MCP_INTERFACE: "MCP",
    API_INTERFACE: "API",
    JOB_INTERFACE: "job",
    CONTROLLER_INTERFACE: "controller",
    INTERNAL_INTERFACE: "internal",
}

# How each door reads beside who came through it: "example, on the web".
DOORS = {
    WEB_INTERFACE: "on the web",
    DEVICE_INTERFACE: "on their own device",
    CLI_INTERFACE: "at the shell",
    MCP_INTERFACE: "through the agent connection",
    API_INTERFACE: "over the API",
    JOB_INTERFACE: "in a background job",
    CONTROLLER_INTERFACE: "through the controller",
    INTERNAL_INTERFACE: "inside HQ",
}


def interface_label(interface: str) -> str:
    """An interface named on its own, as in a list of them: ``mcp`` → "MCP", ``device`` → "device".

    One HQ has not declared reads as its own name.
    """

    return LABELS.get(interface, interface)


def door_words(interface: str) -> str:
    """How ``interface`` reads beside who came through it, or nothing when no door is recorded."""

    if not interface:
        return ""
    return DOORS.get(interface, f"through {interface_label(interface)}")
