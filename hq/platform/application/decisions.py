"""Present the canonical queue without reads or new domain decisions.

Promote the first available step only when its owner supplied no quick action.
Each action renders once; the transport retains its complete workflow.
"""

from __future__ import annotations

from typing import Any


def decision(item: dict[str, Any]) -> dict[str, Any]:
    actions = list(item.get("actions", ()))
    workflow = item.get("workflow")
    if not actions and workflow:
        actions = next(
            (list(step["actions"]) for step in workflow["steps"]
             if step["state"] in {"recommended", "available"} and step["actions"]),
            [],
        )
    identities = {(action["method"], action["url"]) for action in actions}
    if workflow and identities:
        steps = []
        for step in workflow["steps"]:
            remaining = [action for action in step["actions"]
                         if (action["method"], action["url"]) not in identities]
            if remaining or step["summary"]:
                steps.append({**step, "actions": remaining})
        workflow = {**workflow, "steps": steps} if steps else None
    return {**item, "actions": actions, "workflow": workflow}
