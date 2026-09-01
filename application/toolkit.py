"""The tools an operator reaches for, declared once.

A tool is not a new kind of thing: each is backed by registered capabilities,
which already carry the behaviour, the authorization and the other adapters.
This module only says which belong together on a page and in what order.

A registry rather than tabs hard-coded into a template, for the reason
`application.domains` is one: a tool in the markup but not the nav, or the
reverse, is a silent hole rather than a failure. The strip, the routing, the
default and the empty state all derive from one tuple.
"""

from __future__ import annotations

from dataclasses import dataclass

from .integrations import integration_graph
from .security import Principal


@dataclass(frozen=True)
class ToolTab:
    """One tool: what it is called, and which capabilities it runs."""

    id: str
    label: str
    summary: str
    # The capabilities this tab invokes. Named rather than imported so the tab
    # cannot drift from the registry: a tab pointing at a capability that no
    # longer exists fails the contract check below rather than rendering a
    # form that cannot execute.
    capabilities: tuple[str, ...]
    # The partial that draws it. One template per tool, so a tool's markup is
    # not spread through a page that has to know about all of them.
    template: str

    def permitted(self, principal: Principal) -> bool:
        """Whether this principal may run anything this tab offers.

        A tab whose every capability is denied is not shown. Rendering a form
        that can only answer 403 teaches an operator to distrust the page.
        """

        specs = integration_graph().capabilities
        return any(
            principal.permits(*specs[name].required_capabilities)
            for name in self.capabilities
            if name in specs
        )


TOOL_TABS: tuple[ToolTab, ...] = (
    ToolTab(
        id="dns",
        label="DNS",
        summary="Read from outside this network, not through its rewrites.",
        capabilities=("lookup.name", "lookup.address"),
        template="control_plane/_tool_dns.html",
    ),
)


def tabs_for(principal: Principal) -> tuple[ToolTab, ...]:
    return tuple(tab for tab in TOOL_TABS if tab.permitted(principal))


def tab_named(name: str, principal: Principal) -> ToolTab | None:
    """The requested tab, or the first one this principal may use.

    An unknown or forbidden name falls back rather than 404s: the tab is a
    view preference in a query string, and landing on the toolkit with a stale
    bookmark should open the toolkit.
    """

    available = tabs_for(principal)
    for tab in available:
        if tab.id == name:
            return tab
    return available[0] if available else None
