"""For a fix an extension offers a reported problem: the plan, its steps and links, and what closes it.

A link that only opens a page is ``read_link``'s, which gives it no effect or command of its own.
"""

from hq.platform.application.action_links import ActionLink, read_link
from hq.platform.application.workflows import WorkflowOutcome, WorkflowPlan, WorkflowStep

__all__ = ["ActionLink", "WorkflowOutcome", "WorkflowPlan", "WorkflowStep", "read_link"]
