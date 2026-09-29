"""The audit log and one entry in it."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.views.generic import DetailView, ListView

from application import history
from application.pages import PageAction, PageMixin
from application.projection import projection_scope
from application.tables import TableColumn, TableFilter, TableListMixin
from .models import AuditLog


class AuditLogListView(PageMixin, TableListMixin, LoginRequiredMixin, ListView):
    model = AuditLog
    template_name = "core/auditlog_list.html"
    context_object_name = "events"
    paginate_by = 50
    page_title = "Audit log"
    table_search_scope = "audit"
    table_selectable = True
    table_filters = (
        TableFilter("source", "Source", "source", history.SOURCES),
        TableFilter("action", "Action", "action", AuditLog.Action.choices),
    )
    table_columns = (
        TableColumn("When", "created_at", "Oldest event", "Newest event"),
        TableColumn("Who", "user__username", "User A–Z", "User Z–A"),
        TableColumn("Action", "action", "Action", "Action reverse"),
        TableColumn("Object", "object_type", "Object type", "Object type reverse"),
        TableColumn("Message", "message", "Message A–Z", "Message Z–A"),
    )
    table_default_sort = "-created_at"
    table_search_placeholder = "Search objects, operation IDs, and messages…"

    def get_page_actions(self):
        from application.approvals import awaiting_ids

        listing = reverse("core:audit_list")
        awaiting = self._awaiting()
        return (
            PageAction("All events", listing, primary=not awaiting),
            PageAction(
                f"Awaiting approval · {len(awaiting_ids())}",
                f"{listing}?awaiting=1",
                primary=awaiting,
            ),
        )

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user").annotate(source=history.source_of_event())
        if self._awaiting():
            from application.approvals import AUDIT_LABELS, awaiting_ids

            qs = qs.filter(
                action=AuditLog.Action.CREATED,
                object_type__in=AUDIT_LABELS,
                object_id__in=awaiting_ids(),
            )
        return self.apply_table_query(qs)

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
        return span

    def _awaiting(self) -> bool:
        """The log narrowed to requests still waiting for a person."""

        return self.request.GET.get("awaiting") == "1"


class AuditLogDetailView(PageMixin, LoginRequiredMixin, DetailView):
    """One event, in full, and what sits either side of it.

    The list can only ever show a line per event. What an audit trail is
    actually consulted for is the question behind the line (which field
    moved, what the value was before, what else the same action touched) and
    none of that fits in a row. So every row leads here.
    """

    model = AuditLog
    template_name = "core/auditlog_detail.html"
    context_object_name = "event"

    def get_queryset(self):
        return AuditLog.objects.select_related("user")

    def held_approval(self):
        """The approval this event opened, reviewed, if it opened one."""

        if not hasattr(self, "_held_approval"):
            from application import approvals

            held = approvals.for_audit_event(self.object)
            self._held_approval = approvals.review(held) if held is not None else None
        return self._held_approval

    def get_page_title(self):
        return "Approval" if self.held_approval() is not None else "Audit event"

    def get_page_trail(self):
        return (("Audit log", reverse("core:audit_list")),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        event = self.object

        # Field-level changes, as rows rather than as a blob of JSON.
        changes = event.metadata.get("changes") or {}
        context["changes"] = [
            {"field": field, "before": pair[0], "after": pair[1]}
            for field, pair in sorted(changes.items())
            if isinstance(pair, list) and len(pair) == 2
        ]
        # Everything else in the metadata, minus what is already rendered.
        context["extra"] = {
            key: value
            for key, value in sorted(event.metadata.items())
            if key != "changes"
        }

        # The rest of the same operation: what `operation_id` is for. One
        # action can touch many rows, and matching them up by timestamp alone
        # is guesswork.
        if event.operation_id:
            context["siblings"] = (
                AuditLog.objects.filter(operation_id=event.operation_id)
                .exclude(pk=event.pk)
                .select_related("user")[:50]
            )
        # Everything else that ever happened to this object.
        if event.object_type and event.object_id:
            context["history"] = (
                AuditLog.objects.filter(
                    object_type=event.object_type, object_id=event.object_id
                )
                .exclude(pk=event.pk)
                .select_related("user")[:20]
            )
        approval = self.held_approval()
        if approval is not None:
            context["approval"] = approval
        return context
