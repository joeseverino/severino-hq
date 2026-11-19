"""An open page asking for its own readings."""

from typing import override

from django.views import View

from hq.platform.application.asks import AskMixin
from hq.platform.application.visit_refresh import VISIT_REFRESH


class VisitRefreshView(AskMixin, View):
    """Ask for what one page is assembled from: the page says which page it
    is, and what that means is HQ's to work out."""

    capability = VISIT_REFRESH
    back_to = "dashboard"

    @override
    def payload(self):
        return {"subject": self.request.POST.get("subject", ""), "name": self.request.POST.get("name", "")}
