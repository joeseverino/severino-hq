"""An open page asking for its own readings."""

from typing import ClassVar

from django.http import Http404, JsonResponse
from django.views import View

from hq.platform.application.refusal_status import refusal_of
from hq.platform.application.visit_refresh import SUBJECTS, VISIT_REFRESH
from hq.platform.application.web_writes import execute_posted

# A hostname's limit, which is the longest thing a subject is named by.
NAME_LIMIT = 253


class VisitRefreshView(View):
    """Ask for what one page is assembled from.

    The page says which page it is; what that means is HQ's to work out. A POST
    runs ``visit.refresh``, behind the session and the CSRF token, and answers as every ask does: how the read stands, and the signed
    address that says so while it is live (``ReadStatusView``). There is no
    GET, so opening or prefetching a link reads nothing.
    """

    http_method_names: ClassVar[list[str]] = ["post"]

    def post(self, request):
        subject = str(request.POST.get("subject", ""))
        name = str(request.POST.get("name", "")).strip()
        if subject not in SUBJECTS or not name or len(name) > NAME_LIMIT:
            raise Http404("No such page.")
        found = execute_posted(request, VISIT_REFRESH, {"subject": subject, "name": name})
        if not found["ok"]:
            refusal = refusal_of(found)
            return JsonResponse(refusal.answer(), status=refusal.status)
        return JsonResponse(found, status=202 if found["live"] else 200)
