"""Work an extension does outside the process: a network call, a provider.

HQ refuses any connection, subprocess or sleep made while a request is served,
so an extension declares the work as an ``OutboundWork`` and HQ runs it off the
request, deriving its job, capability, route and control (``ask``). Nothing here
lifts the rule. The declaration and what HQ derives from it: docs/Plugins.md,
Outbound work.
"""

from hq.domains.jobs.runner import Failed
from hq.platform.application.outbound_work import OutboundWork, ask, run_now

__all__ = ["Failed", "OutboundWork", "ask", "run_now"]
