"""Held changes and the decision on each."""

from django.contrib import messages
from django.shortcuts import redirect
from django.views import View

from hq.platform.application.approvals import DECISIONS, REJECT
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.web_refusals import refused


class ApprovalDecisionView(View):
    """Agree to a held change, or refuse it. Nothing else can.

    A POST from a signed-in operator runs the ``approval.approve`` or
    ``approval.reject`` capability, which only the browser holds and which
    ``application.approvals`` checks again, so a second surface cannot forget to.
    """

    runs = tuple(DECISIONS)

    def post(self, request, approval_id, decision):
        capability = next((name for name, path in DECISIONS.items() if path == decision), None)
        if capability is None:
            messages.error(request, "Choose whether to approve or reject.")
            return self._back(request)
        payload = {"approval_id": str(approval_id)}
        if capability == REJECT:
            payload["note"] = request.POST.get("note", "")
        # An approval that cannot be applied says why, on the page, with
        # the request left as it was.
        refused(request, execute_posted(request, capability, payload))
        return self._back(request)

    @staticmethod
    def _back(request):
        return redirect(safe_next(request, fallback=f"{reverse('core:audit_list')}?awaiting=1"))
