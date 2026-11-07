"""HQ at a glance as ``dashboard_snapshot`` answers it: what needs doing, from
the operating snapshot without the parts only its page reads.

The page and this answer read one projection (``dashboard.operating_snapshot``).
The answer keeps the figures, the cards and the priority queue's first page,
since every domain, an extension's too, raises items into it, and names, in
place of each list another tool reads (the whole queue, the recent activity,
the active projects, the draft and published content), the call that reads it.
Each queue item keeps every command and address an agent acts on or
points a person at, and leaves out what the page keeps for itself: the
revision a person sets an item aside at, the gate the queue was already
filtered by, the heading items are grouped under, and a plan's own id,
heading and completion, which the page tracks a plan by. A step that says
why HQ cannot do it keeps its reason without the page's heading for it.
"""

from typing import Annotated, Any, TypedDict

from hq.platform.application.dashboard import WITHHELD_ACTIVITY, Kpis, QueueItem, Upstreams, operating_snapshot
from hq.platform.application.item_help import CANNOT
from hq.platform.application.json_schemas import Written
from hq.platform.application.resources import readable, resource_registry
from hq.platform.application.security import Principal
from hq.platform.application.workflow_contracts import WorkflowStep
from hq.platform.application.workflows import WrittenAction

# The queue's first page as an agent is answered it: every domain, an
# extension's too, raises items into the queue, so the glance and
# list_resource answer the most urgent few, and next.cursor the rest. The
# machine API keeps its own page, the default of every list it serves.
ACTION_ITEMS_PAGE = 4

type WrittenStep = Annotated[dict[str, Any], Written(WorkflowStep)]


class AgentQueueItem(TypedDict):
    """One action item: what needs doing, where, and the calls and commands that do it."""

    key: str
    source_id: str
    source: str
    label: str
    detail: str
    count: int
    status: str
    url: str
    action: str
    # The steps of the plan that resolves it, in order; none where it has no plan.
    steps: list[WrittenStep]
    actions: list[WrittenAction]
    subject: WrittenAction | None
    notice: bool
    # ISO 8601, or "" when unknown.
    since: str
    observed_at: str
    # Each a label and its value.
    facts: list[list[str]]
    links: list[WrittenAction]


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
    priority: list[AgentQueueItem]
    # No other read narrows the documentation to the docs due a review.
    docs_needing_review: list[dict[str, Any]]
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


def _step(step: dict[str, Any]) -> dict[str, Any]:
    return {**step, "label": ""} if step["phase"] == CANNOT else step


def agent_item(item: QueueItem) -> AgentQueueItem:
    workflow = item["workflow"]
    return {
        "key": item["key"],
        "source_id": item["source_id"],
        "source": item["source"],
        "label": item["label"],
        "detail": item["detail"],
        "count": item["count"],
        "status": item["status"],
        "url": item["url"],
        "action": item["action"],
        "steps": [_step(step) for step in workflow["steps"]] if workflow else [],
        "actions": item["actions"],
        "subject": item["subject"],
        "notice": item["notice"],
        "since": item["since"],
        "observed_at": item["observed_at"],
        "facts": item["facts"],
        "links": item["links"],
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
        "priority": [agent_item(item) for item in snapshot["priority"][:ACTION_ITEMS_PAGE]],
        "docs_needing_review": snapshot["docs_needing_review"],
        "unread": snapshot["unread"],
        "lists": _lists(principal, snapshot["unread"]),
    }
