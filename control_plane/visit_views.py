"""An open page asking for its own readings."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.views import View

from application.security import web_principal
from application.visit_refresh import SUBJECTS, request_visit_refresh, visit_state

# A hostname's limit, which is the longest thing a subject is named by.
NAME_LIMIT = 253
# Generous for a signed list of kinds, and far short of a request line's limit.
WATCH_LIMIT = 4000


class VisitRefreshView(LoginRequiredMixin, View):
    """Ask for what one page is assembled from, or say whether it is being read.

    The page says which page it is; what that means is HQ's to work out. A POST
    asks, behind the session, the CSRF token and the capability Read now
    needs, and hands back what to watch for, signed. A GET only reports on
    that, so opening or prefetching a link reads nothing and can ask about
    nothing HQ did not hand out.
    """

    http_method_names = ["get", "post"]

    def get(self, request):
        watch = str(request.GET.get("watch", ""))
        found = visit_state(watch) if 0 < len(watch) <= WATCH_LIMIT else None
        if found is None:
            raise Http404("Nothing to watch.")
        return JsonResponse(found)

    def post(self, request):
        subject = str(request.POST.get("subject", ""))
        name = str(request.POST.get("name", "")).strip()
        if subject not in SUBJECTS or not name or len(name) > NAME_LIMIT:
            raise Http404("No such page.")
        found = request_visit_refresh(subject, name, principal=web_principal(request.user))
        if found is None:
            raise Http404("No such page.")
        return JsonResponse(found)
