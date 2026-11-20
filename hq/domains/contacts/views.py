"""Contact submission review screens.

They read the copy the controller last read (``inbox``) and never reach D1. A
review or a deletion waits for the controller to apply it, the message showing
it as waiting until then, and every change is recorded in the audit log.
"""

from functools import cached_property
from typing import Any, override
from urllib.parse import quote

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from hq.domains.control_plane.provider_adapters.cloudflare import SubmissionStatus
from hq.platform.application.asks import WAITING
from hq.platform.application.capabilities import command_spec
from hq.platform.application.command_forms import command_form_class
from hq.platform.application.contact_submissions import (
    DELETE,
    REVIEW,
    change_gap,
    find_submission,
)
from hq.platform.application.labels import dotted
from hq.platform.application.moments import moment, when
from hq.platform.application.pages import PageAction, RecordListMixin, RecordPageMixin, page_context, runs_here
from hq.platform.application.security import Capability, safe_next
from hq.platform.application.ui import counted
from hq.platform.application.url_query import with_query
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.web_refusals import refused

from . import inbox
from .specs import MARKS

GONE = "That message was not found."


def submission_state(submission: dict) -> SubmissionStatus | str:
    """A stored message's status as the member its pill is drawn from, unread
    when it has none; a word the table holds that HQ does not know stays a
    word, drawn in no tone."""

    status = str(submission.get("status") or SubmissionStatus.UNREAD)
    return SubmissionStatus(status) if status in SubmissionStatus.values else status


def reply_mailto(address: str, subject: str = "Re: your message") -> str:
    """A mailto URL whose address and subject cannot add headers.

    The address comes from a public form: CR and LF are stripped, and it and
    the subject are percent-encoded so `?`, `&` and `%` stay literal.
    """

    address = "".join(ch for ch in str(address or "") if ch not in "\r\n")
    subject = "".join(ch for ch in subject if ch not in "\r\n")
    # A mail client reads a "+" in the subject as itself (RFC 6068), so a space
    # is %20 here, never the "+" a web query encodes it as.
    # ast-grep-ignore: query-added-by-with-query
    return f"mailto:{quote(address, safe='@')}?subject={quote(subject, safe='')}"


def _waiting_note(current: inbox.Inbox) -> str:
    if current.refused:
        return f"HQ could not apply the last change: {current.refused}"
    if current.waiting:
        return f"{counted(current.waiting, 'change')}: {WAITING}"
    return ""


def _row_actions(submission: dict) -> tuple[PageAction, ...]:
    """What a message's row offers: the reviews it is not already in, and its
    deletion, which asks first on a page of its own."""

    status = submission.get("status")
    return _offered(
        submission, [mark for mark, shown in (("read", status == "unread"), ("spam", status != "spam")) if shown]
    )


def _page_actions(submission: dict) -> tuple[PageAction, ...]:
    """What a message's page offers: every review it is not already in, and
    its deletion."""

    return _offered(submission, [mark for mark, _words in MARKS if mark != submission.get("status")])


def _offered(submission: dict, marks: list[str]) -> tuple[PageAction, ...]:
    pk = submission["id"]
    offered = (
        *(PageAction.command(REVIEW, pk, variant=mark, field="status", value=mark) for mark in marks),
        PageAction.command(DELETE, pk),
    )
    return tuple(action for action in offered if action is not None)


class ContactListView(RecordListMixin, TemplateView):
    """Every message, under its status's tab and the search."""

    resource = "contact.submissions"
    context_object_name = "submissions"
    required_capability = Capability.MANAGE_CONTACTS
    template_name = "contacts/contact_list.html"
    page_title = "Messages"
    page_lede = "From the site's contact form."
    page_reads = ("cloudflare.contact_submission",)

    @cached_property
    def selected_status(self) -> str:
        status = self.request.GET.get("status", "").strip()
        return status if status in SubmissionStatus.values else ""

    @override
    def list_query(self):
        return {"status": self.selected_status or None, "query": self.request.GET.get("q", "").strip()}

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current = inbox.current()
        selected_status = self.selected_status
        # Everything first, then each status; a value of None is the list with no
        # status asked, and the one current is the one the page shows.
        status_tabs = [
            {"value": value, "label": label, "count": count, "current": (value or "") == selected_status}
            for value, label, count in (
                (None, "All", sum(current.counts.values())),
                *((value, label, current.counts.get(value, 0)) for value, label in SubmissionStatus.choices),
            )
        ]
        for each in self.records:
            each.update(state=submission_state(each), unread=each.get("status") == "unread", actions=_row_actions(each))
        return {
            **context,
            "not_read": current.state == inbox.NOT_READ,
            "status_tabs": status_tabs,
            "selected_status": selected_status,
            "status_kept": {"status": selected_status},
            "q": self.list_query()["query"],
            # The search cleared, on the tab it was asked on.
            "search_cleared": with_query(
                reverse("contacts:list"), {"status": selected_status} if selected_status else None
            ),
            # What the search narrows: the messages of the tab it was asked on.
            "status_total": next(tab["count"] for tab in status_tabs if tab["current"]),
            "total_count": sum(current.counts.values()),
            "problem": current.problem,
            "observed_at": current.observed_at,
            "waiting_note": _waiting_note(current),
            "gap": change_gap(current),
        }


class ContactDetailView(RecordPageMixin, TemplateView):
    """One message, and its review, which posts through the review's capability."""

    resource = "contact.submissions"
    context_object_name = "submission"
    url_kwarg = "pk"
    required_capability = Capability.MANAGE_CONTACTS
    runs = (REVIEW,)
    template_name = "contacts/contact_detail.html"
    page_trail = (("Messages", reverse_lazy("contacts:list")),)
    status = 200
    form: Any = None

    @cached_property
    def review_form(self) -> Any:
        return command_form_class(command_spec(REVIEW), fields=inbox.REVIEWED)

    @override
    def post(self, request, *args, **kwargs):
        if (elsewhere := self.read(request, kwargs)) is not None:
            return elsewhere
        pk = self.record["id"]
        self.form = self.review_form(request.POST)
        if not self.form.is_valid():
            self.status = 400
        elif refusal := refused(
            request, execute_posted(request, REVIEW, self.form.command_payload, target=pk), form=self.form
        ):
            self.status = refusal.status
        else:
            return redirect(self.record["url"])
        return self.render_to_response(self.get_context_data(**kwargs))

    @override
    def get_page_title(self):
        return f"Message #{self.record['id']}"

    @override
    def get_page_lede(self):
        return dotted(self.record.get("name", ""), _sent(self.record))

    @override
    def get_page_actions(self):
        return (PageAction("Reply by email", self.reply),)

    @cached_property
    def reply(self) -> str:
        return reply_mailto(self.record.get("email", ""))

    @override
    def get_context_data(self, **kwargs):
        submission = self.record
        form = self.form or self.review_form(initial={name: submission.get(name) or "" for name in inbox.REVIEWED})
        return super().get_context_data(
            **kwargs,
            state=submission_state(submission),
            reply_href=self.reply,
            form=form,
            gap=change_gap(),
            offered=_page_actions(submission),
            notes_kept=runs_here(REVIEW),
        )

    @override
    def render_to_response(self, context, **response_kwargs):
        return super().render_to_response(context, status=self.status, **response_kwargs)


def _safe_next(request) -> str:
    """Only follow same-host, same-app redirect targets; else back to the list."""
    listing = reverse("contacts:list")
    return safe_next(request, fallback=listing, scope=listing)


@require_POST
def contact_set_status(request, pk: int):
    # The review keeps what it is not sent, so the status is all this sends.
    refused(request, execute_posted(request, REVIEW, {"status": request.POST.get("status", "")}, target=pk))
    return redirect(_safe_next(request))


# Each mark's press; the review as a whole is the message page's form.
contact_set_status.runs = tuple((REVIEW, mark) for mark, _words in MARKS)  # type: ignore[attr-defined]


def contact_delete(request, pk: int):
    submission = find_submission(pk)
    if not submission:
        messages.error(request, GONE)
        return redirect("contacts:list")

    if request.method == "POST":
        result = execute_posted(request, DELETE, {"confirm": str(pk)}, target=pk)
        if refused(request, result):
            return redirect("contacts:detail", pk=pk)
        return redirect("contacts:list")

    return render(
        request,
        "contacts/contact_confirm_delete.html",
        {
            "submission": submission,
            "confirm": {
                "action": PageAction.command(DELETE, pk, confirms=True),
                "cancel_url": reverse("contacts:detail", args=[pk]),
                "gap": change_gap(),
            },
            **page_context(
                f"Delete message #{pk}?",
                dotted(_sent(submission), f"{submission.get('name', '')} ({submission.get('email', '')})"),
                trail=(
                    ("Messages", reverse("contacts:list")),
                    (f"Message #{pk}", reverse("contacts:detail", args=[pk])),
                ),
            ),
        },
    )


def _sent(submission) -> str:
    """When a message was sent, in HQ's time words; D1 writes it in UTC."""

    at = moment(submission.get("created_at"))
    return when(at) if at is not None else ""


contact_delete.runs = (DELETE,)  # type: ignore[attr-defined]
