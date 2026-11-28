"""An open page asking for its own readings."""

from typing import ClassVar, override

from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import render
from django.views import View

from hq.platform.application.asks import AskMixin
from hq.platform.application.visit_refresh import VISIT_REFRESH, visit_standing

# The control a visit is drawn as, which answers its own region.
CONTROL = "partials/_visit_refresh.html"


def _control(request: HttpRequest, source: QueryDict) -> HttpResponse:
    drawn = {name: str(source.get(name, "")) for name in ("subject", "name", "refresh")}
    return render(request, CONTROL, {**drawn, "visit_standing": visit_standing(drawn["subject"], drawn["name"])})


class VisitRefreshView(AskMixin, View):
    """Ask for what one page is assembled from: the page says which page it
    is, and what that means is HQ's to work out. Its control, drawn as it
    stands, is answered here too, so a page following its readings asks for
    that alone."""

    http_method_names: ClassVar[list[str]] = ["get", "post"]
    capability = VISIT_REFRESH
    back_to = "dashboard"

    def get(self, request):
        return _control(request, request.GET)

    @override
    def payload(self):
        return {"subject": self.request.POST.get("subject", ""), "name": self.request.POST.get("name", "")}

    @override
    def answered(self, request):
        return _control(request, request.POST)
