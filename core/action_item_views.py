"""Action items: the list, its unread count, and marking items read."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import TemplateView, View

from application import action_items as read_state
from application.dashboard import work_queue
from application.projection import projection_scope
from application.security import safe_next
from application.pages import PageAction, PageMixin


ACTION_ITEM_FILTERS = ("q", "status", "source")


def _action_items(request, current=None):
    """Every action item with its read state, and those the request's filters keep."""

    if current is None:
        with projection_scope():
            current = work_queue()
    all_items = read_state.with_read_state(current, request.user)
    items = read_state.filter_items(
        all_items,
        query=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        source=request.GET.get("source", ""),
    )
    return all_items, items


class ActionItemsView(PageMixin, LoginRequiredMixin, TemplateView):
    """The full human surface for HQ's one composed attention queue."""

    template_name = "action_items.html"
    page_title = "Action items"

    def get_page_actions(self):
        actions = []
        if self._unread:
            actions.append(PageAction("Mark all read", self._mark_all_url(), method="post"))
        return tuple(actions)

    def _mark_all_url(self) -> str:
        # The filters travel in the URL, so "all" means all that are shown.
        shown = self.request.GET.copy()
        for name in list(shown):
            if name not in ACTION_ITEM_FILTERS:
                del shown[name]
        url = reverse("action_items_read_all")
        if shown:
            url = f"{url}?{shown.urlencode()}"
        return url

    def get_context_data(self, **kwargs):
        all_items, items = _action_items(self.request)
        self._unread = [item for item in items if not item["read"]]
        context = super().get_context_data(**kwargs)
        status = self.request.GET.get("status", "").strip()
        source = self.request.GET.get("source", "").strip()
        sources = tuple(
            {item["source_id"]: item["source"] for item in all_items}.items()
        )
        unread = self._unread
        context.update(
            action_items=unread,
            read_action_items=[item for item in items if item["read"]],
            action_item_total=len(unread),
            profile_action_count=sum(1 for item in all_items if not item["read"]),
            show_action_count=True,
            action_sources=sources,
            action_query=self.request.GET.get("q", "").strip(),
            action_status=status,
            action_source=source,
        )
        return context


class ActionItemCountView(LoginRequiredMixin, View):
    """The unread count for the header, fetched after the page rather than during it."""

    def get(self, request):
        count = read_state.unread_count(work_queue(), request.user)
        return JsonResponse({"count": count})


class ActionItemReadView(LoginRequiredMixin, View):
    """Mark action items read or unread for the signed-in person.

    Each route names the state in ``read``, so a button posts only the item's
    key as its own value.
    """

    read = False

    def post(self, request):
        with projection_scope():
            current = work_queue()
        read_state.mark(
            request.user,
            request.POST.getlist("key"),
            read=self.read,
            current=current,
        )
        return redirect(
            safe_next(request, scope=reverse("action_items"), fallback=reverse("action_items"))
        )


class ActionItemReadAllView(LoginRequiredMixin, View):
    """Mark read every unread item the page's filters (in the query string) show."""

    def post(self, request):
        with projection_scope():
            current = work_queue()
        _, items = _action_items(request, current)
        read_state.mark(
            request.user,
            [item["key"] for item in items if not item["read"]],
            read=True,
            current=current,
        )
        return redirect(
            safe_next(request, scope=reverse("action_items"), fallback=reverse("action_items"))
        )
