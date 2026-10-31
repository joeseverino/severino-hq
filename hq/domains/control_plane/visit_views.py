"""An open page asking for its own readings."""

from typing import ClassVar

from django.http import Http404, JsonResponse
from django.views import View

from hq.platform.application.capabilities import execute_capability
from hq.platform.application.refusal_status import refusal_status
from hq.platform.application.security import web_principal
from hq.platform.application.visit_refresh import SUBJECTS, VISIT_REFRESH

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
        found = execute_capability(
            VISIT_REFRESH, {"subject": subject, "name": name}, principal=web_principal(request.user)
        )
        if not found["ok"] and found["error"]["code"] == "not_found":
            raise Http404("No such page.")
        if not found["ok"]:
            return JsonResponse(found, status=refusal_status(found["error"]["code"]))
        return JsonResponse(found, status=202 if found["live"] else 200)
