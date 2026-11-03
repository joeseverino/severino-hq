"""Held changes and the decision on each."""

from django.contrib import messages
from django.shortcuts import redirect
from django.views import View

from hq.platform.application.approvals import APPROVE, REJECT
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next
from hq.platform.application.web_writes import execute_posted, refused


class ApprovalDecisionView(View):
    """Agree to a held change, or refuse it. Nothing else can.

    A POST from a signed-in operator runs the ``approval.approve`` or
    ``approval.reject`` capability, which only the browser holds and which
    ``application.approvals`` checks again, so a second surface cannot forget to.
    """

    def post(self, request, approval_id, decision=None):
        decision = decision or request.POST.get("decision", "")
        if decision == "approve":
            asked = (APPROVE, {"approval_id": str(approval_id)}, "Approved and applied.")
        elif decision == "reject":
            asked = (
                REJECT,
                {"approval_id": str(approval_id), "note": request.POST.get("note", "")},
                "Rejected. Nothing was applied.",
            )
        else:
            messages.error(request, "Choose whether to approve or reject.")
            return self._back(request)
        name, payload, done = asked
        # An approval that cannot be applied says why, on the page, with
        # the request left as it was.
        if not refused(request, execute_posted(request, name, payload)):
            messages.success(request, done)
        return self._back(request)

    @staticmethod
    def _back(request):
        return redirect(safe_next(request, fallback=f"{reverse('core:audit_list')}?awaiting=1"))
