"""Action items: the queue under its domains, and setting items aside."""

from __future__ import annotations

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import TemplateView, View

from hq.platform.application import action_items as queue_state
from hq.platform.application.dashboard import work_queue
from hq.platform.application.projection import projection_scope
from hq.platform.application.security import safe_next
from hq.platform.application.pages import PageMixin


ACTION_ITEM_FILTERS = ("q", "status", "source")


def _action_items(request, current=None):
    """Every action item with whether it is set aside, and those the request's filters keep."""

    if current is None:
        with projection_scope():
            current = work_queue()
    all_items = queue_state.with_aside_state(current, request.user)
    items = queue_state.filter_items(
        all_items,
        query=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        source=request.GET.get("source", ""),
    )
    return all_items, items


class ActionItemsView(PageMixin, TemplateView):
    """The full human surface for HQ's one composed attention queue."""

    template_name = "action_items.html"
    page_title = "Action items"

    def _aside_all_url(self, part: str) -> str:
        # The filters travel in the URL, so "all" means all that are shown, of
        # the part of the queue the button stands over.
        shown = self.request.GET.copy()
        for name in list(shown):
            if name not in ACTION_ITEM_FILTERS:
                del shown[name]
        shown["part"] = part
        return f'{reverse("action_items_set_aside")}?{shown.urlencode()}'

    def get_context_data(self, **kwargs):
        all_items, items = _action_items(self.request)
        context = super().get_context_data(**kwargs)
        status = self.request.GET.get("status", "").strip()
        source = self.request.GET.get("source", "").strip()
        sources = tuple(
            {item["source_id"]: item["source"] for item in all_items}.items()
        )
        doing, told = queue_state.split_waiting(items)
        context.update(
            aside_all_url=self._aside_all_url("doing"),
            notices_aside_url=self._aside_all_url("told"),
            action_groups=queue_state.by_source(doing),
            action_item_total=len(doing),
            notice_groups=queue_state.by_source(told),
            notice_total=len(told),
            aside_groups=queue_state.by_source([item for item in items if item["aside"]]),
            aside_total=len(items) - len(doing) - len(told),
            profile_action_count=queue_state.count_waiting(all_items),
            show_action_count=True,
            action_sources=sources,
            action_query=self.request.GET.get("q", "").strip(),
            action_status=status,
            action_source=source,
        )
        return context


class ActionItemCountView(View):
    """How many items wait, for the header, fetched after the page rather than during it."""

    def get(self, request):
        count = queue_state.waiting_count(work_queue(), request.user)
        return JsonResponse({"count": count})


class ActionItemAsideView(View):
    """Dismiss items, or restore them, for the signed-in person.

    Each route names the state in ``aside``. A row's button posts the row
    itself (its revision and key), and that is all a dismissal or a restore
    needs: the queue is not composed to handle it. Only "all of these" has to
    know what "these" are: everything the page's filters (in the query string)
    show of the part of the queue it names, or of one family in it.
    A page that asked with a script is answered with nothing; a plain form is
    sent back to the queue.
    """

    aside = False

    def post(self, request):
        rows = queue_state.named_rows(request.POST.getlist("key"))
        if rows and self.aside:
            queue_state.dismiss_rows(request.user, rows)
        elif rows:
            queue_state.restore_rows(request.user, rows)
        elif self.aside:
            with projection_scope():
                current = work_queue()
            _, items = _action_items(request, current)
            doing, told = queue_state.split_waiting(items)
            part = {"doing": doing, "told": told}.get(request.GET.get("part", ""), [])
            family = request.GET.get("family", "")
            if family:
                part = queue_state.of_family(part, family)
            queue_state.set_aside(
                request.user, [item["key"] for item in part], aside=True, current=current
            )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return HttpResponse(status=204)
        return redirect(
            safe_next(request, scope=reverse("action_items"), fallback=reverse("action_items"))
        )
