"""The audit log and one entry in it."""

from typing import override
lazy from datetime import date, datetime, time, timedelta

from django.urls import reverse, reverse_lazy
from django.views.generic import DetailView, ListView
lazy from django.utils import timezone

from hq.platform.application import approvals, history
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.payload_words import STEP, path_words, value_words
from hq.platform.application.projection import day_span, projection_scope
from hq.platform.application.tables import TableColumn, TableFilter, TableListMixin, TableToggle
lazy from hq.platform.application.approvals import AUDIT_LABELS, awaiting_ids

from .models import AuditLog

# The toggle that brings the asks for a fresh reading back into the log.
READS = "reads"


class AuditLogListView(PageMixin, TableListMixin, ListView):
    model = AuditLog
    template_name = "core/auditlog_list.html"
    context_object_name = "events"
    paginate_by = 50
    page_title = "Audit log"
    table_empty_remedy = False
    table_search_scope = "audit"
    table_selectable = True
    table_filters = (
        TableFilter("source", "Where", "source", history.SOURCES),
        TableFilter("action", "Action", "action", AuditLog.Action.choices),
    )
    # Off until asked for: an ask for a fresh reading changes nothing.
    table_toggles = (TableToggle(READS, "Read requests"),)
    # The Message column is prose, so what a row names gets what it leaves.
    table_room = "shared"
    table_columns = (
        TableColumn("What", "object_type", "Type A–Z", "Type Z–A"),
        TableColumn("When", "created_at", "Oldest event", "Newest event", css="date-col key-col"),
        TableColumn("Who", "user__username", "User A–Z", "User Z–A", css="optional-col"),
        TableColumn("Action", "action", "Action A–Z", "Action Z–A"),
        TableColumn("Message", "message", "Message A–Z", "Message Z–A"),
    )
    table_default_sort = "-created_at"
    table_search_placeholder = "Search what changed, ids and messages…"
    page_lede = "Everything that changed, and who or what changed it."

    @override
    def get_page_actions(self):
        listing = reverse("core:audit_list")
        awaiting = self._awaiting()
        waiting = len(awaiting_ids())
        # The way to what waits is offered while something does, and on its
        # own view so there is a way back.
        actions: list[PageAction] = []
        if waiting or awaiting:
            actions.append(PageAction("All events", listing, primary=not awaiting))
            actions.append(
                PageAction(
                    f"Waiting for your approval · {waiting}",
                    f"{listing}?awaiting=1",
                    primary=awaiting,
                )
            )
        # How the work behind these changes went is recorded there.
        actions.append(PageAction("Background jobs", reverse("jobs:list")))
        return tuple(actions)

    @override
    def get_queryset(self):
        qs = AuditLog.objects.select_related("user").annotate(source=history.source_of_event())
        if not self.request.GET.get(READS):
            qs = qs.exclude(object_type=history.read_request_type())
        if self._awaiting():
            qs = qs.filter(
                action=AuditLog.Action.CREATED,
                object_type__in=AUDIT_LABELS,
                object_id__in=awaiting_ids(),
            )
        day = self._day()
        if day is not None:
            start, end = day_span(day, day)
            qs = qs.filter(created_at__gte=start, created_at__lt=end)
        return self.apply_table_query(qs)

    def _day_bounds(self) -> dict:
        day = self._day()
        if day is None:
            return {}
        start = datetime.combine(day, time.min, tzinfo=timezone.get_current_timezone())
        return {"since": start, "until": start + timedelta(days=1)}

    def _day(self):
        """``?on=YYYY-MM-DD``: one day's history, as the calendar opens it."""

        try:
            return date.fromisoformat(self.request.GET.get("on", ""))
        except ValueError:
            return None

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["awaiting"] = self._awaiting()
        page = list(context["object_list"])
        if self._chronological():
            with projection_scope():
                found = history.external(self._external_sources(), **self._span(context, page))
            page = history.entries(page, found)
        else:
            page = history.entries(page, group=False)
        context["object_list"] = context["events"] = page
        return context

    def _chronological(self) -> bool:
        """The log read as history: newest first, unsearched, not a queue.

        Only then do like events run together and the deploys and container
        starts HQ's readings hold take their place between them. A search, a
        sort by another column, an action filter or the approval queue asks for
        the rows themselves.
        """

        get = self.request.GET
        return not (
            self._awaiting()
            or get.get("q", "").strip()
            or get.get("sort", self.table_default_sort) != self.table_default_sort
            or self.table_values("action")
        )

    def _external_sources(self) -> frozenset[str]:
        chosen = self.table_values("source")
        return history.EXTERNAL.intersection(chosen) if chosen else history.EXTERNAL

    def _span(self, context, page) -> dict:
        """The stretch of time this page covers, so each moment lands on one page.

        From the page's oldest event (unbounded on the last page) up to the
        oldest event of the page before (unbounded on the first).
        """

        page_obj = context.get("page_obj")
        if page_obj is None:
            return {}
        span = {}
        if page_obj.has_next() and page:
            span["since"] = page[-1].created_at
        if page_obj.has_previous():
            before = self.object_list[page_obj.start_index() - 2 : page_obj.start_index() - 1]
            span["until"] = next(iter(before.values_list("created_at", flat=True)), None)
        # Within the page's stretch and the chosen day both.
        day = self._day_bounds()
        if day:
            span["since"] = max(filter(None, (span.get("since"), day["since"])))
            span["until"] = min(filter(None, (span.get("until"), day["until"])))
        return span

    @override
    def get_table_empty(self) -> str:
        return "Nothing is waiting for your approval." if self._awaiting() else "Nothing has been recorded yet."

    def _awaiting(self) -> bool:
        """The log narrowed to requests still waiting for a person."""

        return self.request.GET.get("awaiting") == "1"


def _change_rows(changes: dict) -> list[dict]:
    """Field-level changes, as rows rather than as a blob of JSON.

    A plain field is stored as its pair of values; a record or list as the
    values in it that moved, each already in words, by where it is.
    """

    rows = []
    for field, change in changes.items():
        named = path_words((field,))
        if isinstance(change, dict):
            rows.extend(
                {"field": f"{named}{STEP}{inner}" if inner else named, "before": pair[0], "after": pair[1]}
                for inner, pair in change.items()
            )
        elif isinstance(change, list) and len(change) == 2:
            rows.append({"field": named, "before": value_words(change[0]), "after": value_words(change[1])})
    return rows


class AuditLogDetailView(PageMixin, DetailView):
    """One event, in full, and what sits either side of it.

    The list can only ever show a line per event. What an audit trail is
    actually consulted for is the question behind the line (which field
    moved, what the value was before, what else the same action touched) and
    none of that fits in a row. So every row leads here.
    """

    model = AuditLog
    template_name = "core/auditlog_detail.html"
    context_object_name = "event"

    @override
    def get_queryset(self):
        return AuditLog.objects.select_related("user")

    def held_approval(self):
        """The approval this event opened, reviewed, if it opened one."""

        if not hasattr(self, "_held_approval"):
            held = approvals.for_audit_event(self.object)
            self._held_approval = approvals.review(held) if held is not None else None
        return self._held_approval

    @override
    def get_page_title(self):
        return "Approval" if self.held_approval() is not None else "Audit event"

    page_trail = ("Audit log", reverse_lazy("core:audit_list"))

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        event = self.object

        context["changes"] = _change_rows(event.metadata.get("changes") or {})
        # Everything else in the metadata, minus what is already rendered.
        context["extra"] = {
            path_words((key,)): value_words(value) for key, value in sorted(event.metadata.items()) if key != "changes"
        }

        approval = self.held_approval()
        if approval is not None:
            context["approval"] = approval
        context["other_events"] = self._other_events(event, approval is None)
        context["other_rows"] = sum(len(rows) for _label, rows, _folds in context["other_events"])
        return context

    @staticmethod
    def _other_events(event: AuditLog, with_operation: bool) -> list[tuple[str, list[AuditLog], str]]:
        """The other events beside this one, one group each: the rest of its
        operation, then everything else to its object, which folds away under
        the first. Each is ``(label, rows, folds)``."""

        others = AuditLog.objects.exclude(pk=event.pk).select_related("user")
        groups = []
        # The rest of the same operation: what `operation_id` is for. One
        # action can touch many rows, and matching them up by timestamp alone
        # is guesswork. An approval's card already says what it covers.
        if with_operation and event.operation_id:
            siblings = list(others.filter(operation_id=event.operation_id)[:50])
            if siblings:
                groups.append(("Rest of this operation", siblings, ""))
                others = others.exclude(operation_id=event.operation_id)
        if event.object_type and event.object_id:
            to_object = list(others.filter(object_type=event.object_type, object_id=event.object_id)[:20])
            if to_object:
                groups.append(("Everything else to this object", to_object, "audit-object-history" if groups else ""))
        return groups
