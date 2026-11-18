"""HQ at a glance as ``dashboard_snapshot`` answers it: what needs doing, from
the operating snapshot without the parts only its page reads.

The page and this answer read one projection (``dashboard.operating_snapshot``).
The answer keeps the figures, the cards and the priority queue's first page,
since every domain, an extension's too, raises items into it, and names, in
place of each list another tool reads (the whole queue, the recent activity,
the active projects, the draft and published content), the call that reads it.
Each queue item is the one a list of action items holds (``dashboard.listed_item``).
"""

from typing import Any, TypedDict

from hq.platform.application.dashboard import WITHHELD_ACTIVITY, Kpis, Upstreams, listed_item, operating_snapshot
from hq.platform.application.resource_records import ActionItem
from hq.platform.application.resources import readable, resource_registry
from hq.platform.application.security import Principal

# The queue's first page as an agent is answered it: every domain, an
# extension's too, raises items into the queue, so the glance and
# list_resource answer the most urgent few, and next.cursor the rest. The
# machine API keeps its own page, the default of every list it serves.
ACTION_ITEMS_PAGE = 4


class ListRead(TypedDict):
    """A list the glance leaves to the tool that reads it: send its ``arguments`` to its ``tool``."""

    tool: str
    arguments: dict[str, Any]


class AgentSnapshot(TypedDict):
    """What ``dashboard_snapshot`` answers."""

    generated_at: str
    upstreams: Upstreams
    year: int
    kpis: Kpis
    cards: list[dict[str, Any]]
    # How many items the whole queue holds.
    priority_count: int
    # Its first page, the most urgent; ``lists.priority`` reads the rest.
    priority: list[ActionItem]
    # Each part this caller may not read, said, so withheld and empty read apart.
    unread: list[str]
    # Each list the page shows that this answer leaves to its own read, or
    # holds only the first page of, by the page's name for it; one this caller
    # may not read is left out.
    lists: dict[str, ListRead]


# The page's lists another tool reads, each by the resource whose list it is.
_LISTED: dict[str, tuple[str, dict[str, Any]]] = {
    "priority": ("action_items", {}),
    "active_projects": ("projects", {"status": "active"}),
    "draft_content": ("content", {"status": "draft"}),
    "recent_published": ("content", {"status": "published"}),
}


def _lists(principal: Principal, unread: list[str]) -> dict[str, ListRead]:
    registry = resource_registry()
    found: dict[str, ListRead] = {}
    if WITHHELD_ACTIVITY not in unread:
        found["recent_activity"] = {"tool": "recent_activity", "arguments": {}}
    for part, (name, filters) in _LISTED.items():
        if readable(registry[name], principal):
            found[part] = {"tool": "list_resource", "arguments": {"name": name, "filters": filters}}
    return found


def agent_snapshot(*, principal: Principal) -> AgentSnapshot:
    """The operating snapshot as an agent reads it: what needs doing, and where each of its lists is read."""

    snapshot = operating_snapshot(principal=principal)
    return {
        "generated_at": snapshot["generated_at"],
        "upstreams": snapshot["upstreams"],
        "year": snapshot["year"],
        "kpis": snapshot["kpis"],
        "cards": snapshot["cards"],
        "priority_count": snapshot["priority_count"],
        "priority": [listed_item(item) for item in snapshot["priority"][:ACTION_ITEMS_PAGE]],
        "unread": snapshot["unread"],
        "lists": _lists(principal, snapshot["unread"]),
    }
