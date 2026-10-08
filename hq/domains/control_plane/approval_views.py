"""Held changes and the decision on each."""

from django.contrib import messages

from django.shortcuts import redirect
from hq.platform.application.routes import reverse
from django.views import View

from hq.platform.application.security import AuthorizationError, safe_next, web_principal


class ApprovalListView(View):
    """Retired: redirects to the audit log's awaiting view."""

    def get(self, request):
        return redirect(f"{reverse('core:audit_list')}?awaiting=1")


class ApprovalDecisionView(View):
    """Agree to a held change, or refuse it. Nothing else can.

    A POST from a signed-in operator, which is the entire mechanism: the
    interface is the check, and ``application.approvals`` makes it rather than
    this view, so a second surface cannot forget to.
    """

    def post(self, request, approval_id, decision=None):
        from hq.platform.application import approvals

        decision = decision or request.POST.get("decision", "")
        try:
            if decision == "approve":
                approvals.approve(str(approval_id), principal=web_principal(request.user))
                messages.success(request, "Approved and applied.")
            elif decision == "reject":
                approvals.reject(
                    str(approval_id),
                    principal=web_principal(request.user),
                    note=request.POST.get("note", ""),
                )
                messages.success(request, "Rejected. Nothing was applied.")
            else:
                messages.error(request, "Choose whether to approve or reject.")
        except (AuthorizationError, ValueError) as exc:
            # One handler for both, because a person reading this page is owed
            # the same treatment either way: an approval that cannot be applied
            # says why, on the page, with the request left as it was.
            messages.error(request, str(exc) or "Could not record that decision.")
        return redirect(
            safe_next(request, fallback=f"{reverse('core:audit_list')}?awaiting=1")
        )
