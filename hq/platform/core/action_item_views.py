"""Action items: the queue under its domains, and setting items aside."""

from __future__ import annotations

import hashlib
import re
import time

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.http import condition
from django.views.generic import TemplateView, View

from hq.platform.application import action_items as queue_state
from hq.platform.application.dashboard import work_queue
from hq.platform.application.derivations import table_revisions
from hq.platform.application.domains import attention_key, attention_standing
from hq.platform.core.models import ActionItemRead
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


def _count_base(request) -> str | None:
    """What the header's count is derived from now; None when that is not known.

    The key the queue is answered under, the person, and the revision of their
    set-aside rows.
    """

    queue = attention_key()
    aside = table_revisions((ActionItemRead._meta.db_table,))
    if queue is None or aside is None:
        return None
    return hashlib.sha256(f"{queue}|{request.user.pk}|{aside[0]}".encode()).hexdigest()


# A validator: what the count was derived from, then the second it stops holding.
_VALIDATOR = re.compile(r'^"([0-9a-f]{64})-(\d{1,12})"$')


def _count_etag(request) -> str | None:
    """The validator the request presented, if it still vouches for the count.

    It does when nothing the count reads has been written since and the moment
    the answer stops holding has not come. Anything else is None, and the count
    is composed: an unknown state is never answered "not modified".
    """

    presented = _VALIDATOR.match(request.headers.get("If-None-Match", ""))
    if presented is None or presented[1] != _count_base(request):
        return None
    if time.time() >= int(presented[2]):
        return None
    return presented[0]


def _count_validator(request) -> str | None:
    """The validator for the count this request just composed."""

    base, queue = _count_base(request), attention_standing()
    if base is None or queue is None:
        return None
    # An answer with no moment of its own is asked about again within the day.
    until = int(queue.until.timestamp()) if queue.until else int(time.time()) + 24 * 60 * 60
    return f'"{base}-{until}"'


@method_decorator(condition(etag_func=_count_etag), name="get")
class ActionItemCountView(View):
    """How many items wait, for the header, fetched after the page rather than during it.

    A request carrying the validator of an answer that still stands is
    answered 304 from the table revisions alone, without composing the queue.
    """

    def get(self, request):
        count = queue_state.waiting_count(work_queue(), request.user)
        response = JsonResponse({"count": count})
        validator = _count_validator(request)
        if validator:
            response.headers["ETag"] = validator
        # Kept by the browser, and asked about before every reuse.
        response.headers["Cache-Control"] = "private, no-cache"
        return response


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
