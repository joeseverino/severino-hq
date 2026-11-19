"""Contact submission review screens.

They read the copy the controller last read (``inbox``) and never reach D1. A
review or a deletion waits for the controller to apply it, the message showing
it as waiting until then, and every change is recorded in the audit log.
"""

from urllib.parse import quote

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from hq.platform.application.asks import WAITING
from hq.platform.application.capabilities import command_spec
from hq.platform.application.contact_submissions import (
    DELETE,
    MOST_SHOWN,
    REVIEW,
    change_gap,
    find_submission,
    list_contact_submissions,
)
from hq.platform.application.labels import dotted
from hq.platform.application.moments import moment, when
from hq.platform.application.pages import PageAction, page_context, runs_here
from hq.platform.application.security import safe_next
from hq.platform.application.ui import counted
from hq.platform.application.url_query import with_query
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.web_refusals import refused

from . import inbox
from .forms import STATUS_CHOICES, ContactReviewForm, submission_state

STATUS_LABELS = dict(STATUS_CHOICES)
GONE = "That message was not found."


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

    marks = (mark for mark, _words in command_spec(REVIEW).variants)
    return _offered(submission, [mark for mark in marks if mark != submission.get("status")])


def _offered(submission: dict, marks: list[str]) -> tuple[PageAction, ...]:
    pk = submission["id"]
    offered = (
        *(PageAction.command(REVIEW, pk, variant=mark, field="status", value=mark) for mark in marks),
        PageAction.command(DELETE, pk),
    )
    return tuple(action for action in offered if action is not None)


def contact_list(request):
    selected_status = request.GET.get("status", "").strip()
    if selected_status not in STATUS_LABELS:
        selected_status = ""
    q = request.GET.get("q", "").strip()
    current = inbox.current()
    submissions = list_contact_submissions(status=selected_status, query=q, limit=MOST_SHOWN)["items"]
    # Everything first, then each status; a value of None is the list with no
    # status asked, and the one current is the one the page shows.
    status_tabs = [
        {"value": value, "label": label, "count": count, "current": (value or "") == selected_status}
        for value, label, count in (
            (None, "All", sum(current.counts.values())),
            *((value, label, current.counts.get(value, 0)) for value, label in STATUS_CHOICES),
        )
    ]

    return render(
        request,
        "contacts/contact_list.html",
        {
            "not_read": current.state == inbox.NOT_READ,
            "submissions": [
                {
                    **each,
                    "state": submission_state(each),
                    "unread": each.get("status") == "unread",
                    "actions": _row_actions(each),
                }
                for each in submissions
            ],
            "status_tabs": status_tabs,
            "selected_status": selected_status,
            "status_kept": {"status": selected_status},
            "q": q,
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
            **page_context("Messages", "From the site's contact form.", reads=contact_list.page_reads),
        },
    )


contact_list.page_reads = ("cloudflare.contact_submission",)  # type: ignore[attr-defined]


def _review(request, pk: int, review: dict[str, str]) -> None:
    """Queue one review; say why not when it is refused."""

    refused(request, execute_posted(request, REVIEW, review, target=pk))


def contact_detail(request, pk: int):
    submission = find_submission(pk)
    if not submission:
        messages.error(request, GONE)
        return redirect("contacts:list")

    if request.method == "POST":
        form = ContactReviewForm(request.POST)
        if form.is_valid():
            _review(request, pk, dict(form.cleaned_data))
            return redirect("contacts:detail", pk=pk)
    else:
        form = ContactReviewForm(
            initial={
                "status": submission.get("status") or "unread",
                "assigned_to": submission.get("assigned_to") or "",
                "admin_notes": submission.get("admin_notes") or "",
            }
        )

    reply = reply_mailto(submission.get("email", ""))
    return render(
        request,
        "contacts/contact_detail.html",
        {
            "submission": submission,
            "state": submission_state(submission),
            "reply_href": reply,
            "form": form,
            "gap": change_gap(),
            "offered": _page_actions(submission),
            "notes_kept": runs_here(REVIEW),
            **page_context(
                f"Message #{pk}",
                dotted(submission.get("name", ""), _sent(submission)),
                actions=(PageAction("Reply by email", reply),),
                trail=(("Messages", reverse("contacts:list")),),
            ),
        },
    )


def _safe_next(request) -> str:
    """Only follow same-host, same-app redirect targets; else back to the list."""
    listing = reverse("contacts:list")
    return safe_next(request, fallback=listing, scope=listing)


@require_POST
def contact_set_status(request, pk: int):
    status = request.POST.get("status", "")
    if status not in STATUS_LABELS:
        messages.error(request, "Choose a status from the list.")
        return redirect(_safe_next(request))

    submission = find_submission(pk)
    if not submission:
        messages.error(request, GONE)
        return redirect("contacts:list")
    review = {
        "status": status,
        "assigned_to": submission.get("assigned_to") or "",
        "admin_notes": submission.get("admin_notes") or "",
    }
    _review(request, pk, review)
    return redirect(_safe_next(request))


contact_set_status.runs = (REVIEW,)  # type: ignore[attr-defined]


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
