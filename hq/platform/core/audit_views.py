"""The audit log and one entry in it."""

from functools import cached_property
from typing import override

from django.core.exceptions import ValidationError
from django.urls import reverse, reverse_lazy
from django.views.generic import DetailView, TemplateView

from hq.platform.application import approval_review, approvals, history
from hq.platform.application.audit_specs import audit_rows
from hq.platform.application.doors import door_words
from hq.platform.application.labels import dotted
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.moments import stamp_day
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.payload_words import path_words, value_words
from hq.platform.application.projection import day_span, projection_scope
from hq.platform.application.security import Capability, web_principal
from hq.platform.application.tables import TableColumn
from hq.platform.application.url_query import with_query
lazy from hq.domains.control_plane.models import OperationRequest, SourceProposal
lazy from hq.platform.application.approvals import awaiting_ids
lazy from hq.platform.application.automation_adoption import procedures as adopted_procedures
lazy from hq.platform.application.automation_sources import adopted_revision, decisions, of_audit_entry, playbook_table

from .audit import audited_labels, audited_subjects, subject_of
from .audit_words import change_rows, words_of
from .models import AuditLog

# ``?about=<key>``: the log narrowed to every row about one record.
ABOUT = "about"


class AuditLogListView(ListPageMixin, TemplateView):
    """The ``audit`` list read as one history: each operation one line, what
    it changed folded beneath it, and the deploys and container starts HQ's
    readings hold between them."""

    # A security log: read apart from the baseline READ a viewer holds.
    required_capability = Capability.READ_AUDIT_LOG
    resource = "audit"
    template_name = "list_page.html"
    page_size = 50
    page_title = "Audit log"
    table_empty_remedy = False
    table_selectable = True
    # The columns every table of history draws, each sorted here by its field.
    table_columns = tuple(
        TableColumn(label, sort, css=css)
        for (label, css), sort in zip(
            history.COLUMNS, ("object_type", "created_at", "user__username", "action", "message"), strict=True
        )
    )
    table_search_placeholder = "Search what changed, ids and messages…"

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
        return f"Events about {about}." if about else self.page_lede

    @override
    def list_query(self):
        """Read as history, newest first, unsearched, not a queue, of every
        action and of no one chain, a page is a page of events, one line per
        chain; one chain asked is every row of it."""

        asked = super().list_query()
        default = self.listing.sorts[0].name
        history_read = not (
            self._awaiting()
            or asked.get("query")
            or asked.get("sort", default) != default
            or asked.get("action")
            or asked.get(history.CHAIN)
        )
        return {**asked, "events": True} if history_read else asked

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return {**context, "awaiting": self._awaiting(), "events": context["object_list"]}

    @cached_property
    def _entries(self) -> tuple:
        page = self.records(AuditLog.objects.select_related("user"))
        asked = self.list_query()
        if not asked.get("events"):
            return tuple(history.entries(page))
        # What each operation changed is read from the rows its filters keep.
        within = audit_rows(about=self._about(), on=self._day()).filter(self.listing.keeps(asked, searched=False))
        with projection_scope():
            moments = history.external(self._external_sources(asked), **self._span(page, within))
        return tuple(history.entries(page, moments, within=within))

    @override
    def rows(self, items):
        """Each event, then what it changed, folded beneath it in the same columns."""

        return [line for event in self._entries for line in event.lines]

    @override
    def drawn(self, label, table, rows):
        return history.table(
            label,
            (history.HistoryGroup(self._entries),),
            table["columns"],
            selectable=table["selectable"],
            sorted_by=table["selected_sort"],
        )

    @override
    def table(self):
        if self._about():
            empty = f"Nothing has been recorded about {self._about()}."
        else:
            empty = "Nothing is waiting for your approval." if self._awaiting() else "Nothing has been recorded yet."
        return {**super().table(), "empty": empty}

    def _external_sources(self, asked) -> frozenset[str]:
        chosen = set(asked.get("source", "").split(",")) - {""}
        return history.EXTERNAL.intersection(chosen) if chosen else history.EXTERNAL

    def _span(self, page, within) -> dict:
        """The stretch of time this page covers, so each moment lands on one page.

        From the page's oldest event (unbounded on the last page) up to the
        oldest event of the page before (unbounded on the first).
        """

        read, span = self.collection, {}
        if read["next"] and page:
            span["since"] = page[-1].created_at
        if read["previous"]:
            start = read["previous"] * self.page_size
            before = history.heads(within).order_by(*self.listing.sorts[0].ordering)[start - 1 : start]
            span["until"] = next(iter(before.values_list("created_at", flat=True)), None)
        # Within the page's stretch and the chosen day both.
        if (day := self._day()) is not None:
            since, until = day_span(day, day)
            span["since"] = max(filter(None, (span.get("since"), since)))
            span["until"] = min(filter(None, (span.get("until"), until)))
        return span

    def _day(self):
        """``?on=YYYY-MM-DD``: one day's history, as the calendar opens it."""

        return stamp_day(self.request.GET.get("on", ""))

    def _about(self) -> str:
        """``?about=<key>``: the record whose whole story the log is narrowed to."""

        return self.request.GET.get(ABOUT, "").strip()

    def _awaiting(self) -> bool:
        """The log narrowed to requests still waiting for a person."""

        return self.request.GET.get("awaiting") == "1"


# The metadata an event's page says in rows of their own rather than under
# "Metadata": who acted, through which door and who approved it are the Who row's.
# What started it is the chain's, and the record it is about the About row's.
_STATED_ABOVE = frozenset(
    {"changes", "operation", "actor", "interface", "approved_by", "approval", "cause", "trace", "subject"}
)


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

    return with_query(reverse("core:audit_list"), {ABOUT: key})


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
        return approval_review.review(held, web_principal(self.request.user)) if held is not None else None

    @cached_property
    def scan(self):
        """The scan result this event records being read, reviewed, if it records one."""

        return of_audit_entry(self.object)

    @override
    def get_page_title(self):
        return "Approval" if self.held_approval is not None else "Audit event"

    @override
    def get_page_actions(self):
        # A scan's read, while it waits: Adopt all and Reject all.
        return decisions(self.scan, web_principal(self.request.user), diff=False)

    page_trail = (("Audit log", reverse_lazy("core:audit_list")),)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        event = self.object

        metadata = event.metadata or {}
        # What happened, and to what kind of thing, as the event's heading.
        context["event_heading"] = dotted(event.get_action_display(), event.object_type and event.type_label)
        context["changes"] = approval_review.ChangePreview(
            label="Changes", rows=change_rows(metadata.get("changes")), lines=(), made=True
        )
        # The operation's name and id are one fact, stated in its own row.
        context["operation"] = operation_words(event)
        # A row about the operation itself has its id as its object's, already said there.
        context["operation_id"] = event.operation_id if event.operation_id != event.object_id else ""
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
        if (scan := self.scan) is not None:
            proposal = scan.proposal
            refreshed, rejected = history.who_did_each(
                history.asking(OperationRequest, (proposal.operation,)),
                history.asking(SourceProposal, (proposal,), changed="rejected_at"),
            )
            context.update(
                scan=scan,
                refreshed_by=refreshed.get(str(proposal.operation_id)),
                rejected_by=rejected.get(str(proposal.pk)),
                adopted_revision=adopted_revision(),
                # Each playbook it changes, with Adopt and Reject while it waits.
                scan_rows=playbook_table(adopted_procedures(), scan, web_principal(self.request.user), every=False),
            )
        context["other_events"] = self._other_events(event, with_chain=approval is None)
        return context

    def _other_events(self, event: AuditLog, *, with_chain: bool) -> list[history.HistoryGroup]:
        """The other events beside this one, one group each, as lines of the
        history: the chain it is part of, each of its operations a line with
        what it changed beneath it, then everything else to its object, which
        folds away under the first. An approval's card already says what it
        covers."""

        others = AuditLog.objects.exclude(pk=event.pk).select_related("user")
        groups = []
        if with_chain and event.trace:
            steps = history.chain(event.trace)
            groups.append(history.HistoryGroup(steps, history.EVENTS))
            others = others.exclude(trace=event.trace)
        if event.object_type and event.object_id:
            to_object = list(others.filter(object_type=event.object_type, object_id=event.object_id)[:20])
            if to_object:
                groups.append(
                    history.HistoryGroup(
                        tuple(history.entries(to_object)),
                        "Everything else to this object",
                        "audit-object-history" if groups else "",
                    )
                )
        return groups
