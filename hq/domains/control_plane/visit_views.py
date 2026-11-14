"""An open page asking for its own readings."""

from typing import override

from django.http import Http404
from django.views import View

from hq.platform.application.asks import AskMixin, Standing, Took
from hq.platform.application.visit_refresh import VISIT_REFRESH, reads_of

# A hostname's limit, which is the longest thing a subject is named by.
NAME_LIMIT = 253


class VisitRefreshView(AskMixin, View):
    """Ask for what one page is assembled from: the page says which page it
    is, and what that means is HQ's to work out."""

    capability = VISIT_REFRESH
    back_to = "dashboard"

    @override
    def payload(self):
        subject = str(self.request.POST.get("subject", ""))
        name = str(self.request.POST.get("name", "")).strip()
        if len(name) > NAME_LIMIT or reads_of(subject, name) is None:
            raise Http404("No such page.")
        return {"subject": subject, "name": name}

    @override
    def asked(self, result, at) -> Took:
        return Took(Standing.of(result), result["status"])
