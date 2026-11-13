"""The audit log and one entry in it."""

from functools import cached_property
from typing import override
from urllib.parse import urlencode
lazy from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.core.paginator import Page, Paginator
from django.db.models import BooleanField, ExpressionWrapper, F, Q
from django.urls import reverse, reverse_lazy
from django.views.generic import DetailView, ListView
lazy from django.utils import timezone

from hq.platform.application import approvals, history
from hq.platform.application.doors import door_words
from hq.platform.application.labels import dotted
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.payload_words import path_words, value_words
from hq.platform.application.projection import day_span, projection_scope
from hq.platform.application.record_timeline import record_timeline
from hq.platform.application.security import Capability
from hq.platform.application.tables import (
    TableColumn,
    TableFilter,
    TableListMixin,
    TableToggle,
    UncountedPaginator,
)
lazy from hq.platform.application.approvals import AUDIT_LABELS, awaiting_ids

from .audit import audited_labels, audited_subjects, subject_of
from .audit_words import change_rows, words_of
from .models import AuditLog

# The toggles that bring the asks for a fresh reading, and the work HQ did on
# its own that changed nothing, back into the log.
READS = "reads"
ROUTINE = "routine"
# ``?about=<key>``: the log narrowed to every row about one record.
ABOUT = "about"
# How many of the rest of an operation an event's page lists at a time.
OPERATION_PAGE = 50


class AuditLogListView(PageMixin, TableListMixin, ListView):
    # A security log: read apart from the baseline READ a viewer holds.
    required_capability = Capability.READ_AUDIT_LOG
    model = AuditLog
    template_name = "core/auditlog_list.html"
    context_object_name = "events"
    paginate_by = 50
    # Fifty events a page, and never a count of every row on each load.
    paginator_class = UncountedPaginator
    page_title = "Audit log"
    table_empty_remedy = False
    table_search_scope = "audit"
    table_selectable = True
    table_filters = (
        TableFilter("source", "Where", "source", history.SOURCES),
        TableFilter("action", "Action", "action", AuditLog.Action.choices),
    )
    # Off until asked for: an ask for a fresh reading changes nothing.
    table_toggles = (TableToggle(READS, "Read requests"), TableToggle(ROUTINE, "Routine work"))
    # The Message column is prose, so what a row names gets what it leaves.
    table_room = "shared"
    table_columns = (
        TableColumn("Type", "object_type", "Type A–Z", "Type Z–A"),
        TableColumn("Time", "created_at", "Oldest event", "Newest event", css="date-col key-col"),
        TableColumn("User", "user__username", "User A–Z", "User Z–A", css="optional-col"),
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
        if self._about():
            actions.append(PageAction("All events", listing))
        elif waiting or awaiting:
            actions.append(PageAction("All events", listing, primary=not awaiting))
            actions.append(
                PageAction(
                    dotted("Waiting for your approval", waiting),
                    f"{listing}?awaiting=1",
                    primary=awaiting,
                )
            )
        # How the work behind these changes went is recorded there.
        actions.append(PageAction("Background jobs", reverse("jobs:list")))
        return tuple(actions)

    @override
    def get_page_lede(self) -> str:
        about = self._about()
        return f"Everything that happened to {about}, and who or what did it." if about else self.page_lede

    @override
    def get_queryset(self):
        rows = self._rows
        # Read as history, a page is a page of events, one line per operation.
        return history.heads(rows) if self._chronological() else rows

    @cached_property
    def _rows(self):
        """The rows this view of the log shows, before they are read as events."""

        about = self._about()
        qs = record_timeline(about) if about else AuditLog.objects.all()
        qs = qs.select_related("user").annotate(source=history.source_of_event())
        if not self.request.GET.get(READS):
            qs = qs.exclude(history.read_requests())
        if not self.request.GET.get(ROUTINE):
            qs = qs.exclude(history.routine())
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
            lines = history.entries(page, found, within=self._rows)
        else:
            lines = history.entries(page)
        # Each event, then what it changed, folded beneath it in the same columns.
        context["object_list"] = context["events"] = [line for event in lines for line in event.lines]
        return context

    def _chronological(self) -> bool:
        """The log read as history: newest first, unsearched, not a queue.

        Only then is each operation one line, with what it changed beneath
        it, and the deploys and container starts HQ's readings hold take their
        place between them. A search, a
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
        if self._about():
            return f"Nothing has been recorded about {self._about()}."
        return "Nothing is waiting for your approval." if self._awaiting() else "Nothing has been recorded yet."

    def _about(self) -> str:
        """``?about=<key>``: the record whose whole story the log is narrowed to."""

        return self.request.GET.get(ABOUT, "").strip()

    def _awaiting(self) -> bool:
        """The log narrowed to requests still waiting for a person."""

        return self.request.GET.get("awaiting") == "1"


# The metadata an event's page says in rows of their own rather than under
# "Metadata": who acted and through which door are the Who row's.
# What started it and the record it is about are the Cause and About rows'.
_STATED_ABOVE = frozenset({"changes", "operation", "actor", "interface", "approved_by", "cause", "subject"})


def operation_words(event: AuditLog) -> str:
    """How an event's page names the operation ``event`` was written in: in the
    words that declared it, else as a request through its door, never by its route."""

    if not (event.metadata or {}).get("operation"):
        return ""
    return words_of(event).title or f"A request {door_words(event.interface)}"


def _subject(event: AuditLog) -> str:
    """The key of the record ``event`` is about: the one it stored, else, for a
    row written before rows named one, its object's while that object exists."""

    if event.subject or not event.object_id:
        return event.subject
    labels = audited_labels()
    for model in audited_subjects():
        if labels[model] == event.object_type:
            try:
                return subject_of(model._default_manager.filter(pk=event.object_id).first()) or ""
            except ValueError, ValidationError:  # an id its model's key cannot hold
                return ""
    return ""


def about_url(key: str) -> str:
    """The log narrowed to one record's whole story (``?about=``)."""

    return f"{reverse('core:audit_list')}?{urlencode({ABOUT: key})}"


def _cause(event: AuditLog) -> tuple[str, str] | None:
    """The operation that started this row's own, as its name and the page that reads it.

    That page is the operation's own row's (a queued request's, an approval's),
    else its newest row's, which lists the rest of it. A row written before
    rows named their cause has none.
    """

    if not event.cause:
        return None
    own = ExpressionWrapper(Q(object_id=F("operation_id")), output_field=BooleanField())
    started = (
        AuditLog.objects.filter(operation_id=event.cause)
        .annotate(own_row=own)
        .order_by("-own_row", "-created_at", "-pk")
        .first()
    )
    if started is None:
        return None
    named = started.object_repr if started.own_row and started.object_repr else operation_words(started)
    return named or started.summary, reverse("core:audit_detail", args=[started.pk])


class AuditLogDetailView(PageMixin, DetailView):
    """One event, in full, and what sits either side of it.

    The list can only ever show a line per event. What an audit trail is
    actually consulted for is the question behind the line (which field
    moved, what the value was before, what else the same action touched) and
    none of that fits in a row. So every row leads here.
    """

    required_capability = Capability.READ_AUDIT_LOG
    model = AuditLog
    template_name = "core/auditlog_detail.html"
    context_object_name = "event"

    @override
    def get_queryset(self):
        return AuditLog.objects.select_related("user")

    @cached_property
    def held_approval(self):
        """The approval this event opened, reviewed, if it opened one."""

        held = approvals.for_audit_event(self.object)
        return approvals.review(held) if held is not None else None

    @override
    def get_page_title(self):
        return "Approval" if self.held_approval is not None else "Audit event"

    page_trail = (("Audit log", reverse_lazy("core:audit_list")),)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        event = self.object

        metadata = event.metadata or {}
        # What happened, and to what kind of thing, as the event's heading.
        context["event_heading"] = dotted(event.get_action_display(), event.object_type and event.type_label)
        context["changes"] = change_rows(metadata.get("changes"))
        # The operation's name and id are one fact, stated in its own row.
        context["operation"] = operation_words(event)
        # A row about the operation itself has its id as its object's, already said there.
        context["operation_id"] = event.operation_id if event.operation_id != event.object_id else ""
        # Who acted is the Who row; the recorded actor is said beside it only where it differs.
        actor = str(metadata.get("actor") or "")
        context["acting_as"] = actor if actor != event.actor_label else ""
        context["door"] = door_words(event.interface)
        approver = str(metadata.get("approved_by") or "")
        context["approved_by"] = approver if approver != event.actor_label else ""
        context["cause"] = _cause(event)
        if subject := _subject(event):
            context["about"] = (subject, about_url(subject))
        # Everything else in the metadata, minus what those rows already say.
        context["extra"] = {
            path_words((key,)): value_words(value)
            for key, value in sorted(metadata.items())
            if key not in _STATED_ABOVE
        }

        approval = self.held_approval
        if approval is not None:
            context["approval"] = approval
        groups, page = self._other_events(event, with_operation=approval is None)
        context["other_events"] = groups
        # The list a history line's count of the rest opens to.
        context["operation_list"] = history.OPERATION_LIST
        context["other_rows"] = sum(len(rows) for _label, rows, _count, _folds in groups)
        # The rest of the operation is every row of it, a page at a time.
        context.update(
            page_obj=page,
            paginator=page.paginator if page else None,
            is_paginated=bool(page and page.has_other_pages()),
        )
        return context

    def _other_events(
        self, event: AuditLog, *, with_operation: bool
    ) -> tuple[list[tuple[str, list[AuditLog], int, str]], Page | None]:
        """The other events beside this one, one group each: the rest of its
        operation, a page of it at a time, then everything else to its object,
        which folds away under the first. Each is ``(label, rows, count,
        folds)``, beside the page of the operation shown."""

        others = AuditLog.objects.exclude(pk=event.pk).select_related("user")
        groups = []
        page = None
        # The rest of the same operation: what `operation_id` is for. One
        # action can touch many rows, and matching them up by timestamp alone
        # is guesswork. An approval's card already says what it covers.
        if with_operation and event.operation_id:
            siblings = others.filter(operation_id=event.operation_id).order_by("-created_at", "-pk")
            page = Paginator(siblings, OPERATION_PAGE).get_page(self.request.GET.get("page"))
            if page.object_list:
                groups.append(("Rest of this operation", list(page.object_list), page.paginator.count, ""))
                others = others.exclude(operation_id=event.operation_id)
            else:
                page = None
        if event.object_type and event.object_id:
            to_object = list(others.filter(object_type=event.object_type, object_id=event.object_id)[:20])
            if to_object:
                groups.append(
                    (
                        "Everything else to this object",
                        to_object,
                        len(to_object),
                        "audit-object-history" if groups else "",
                    )
                )
        return groups, page
