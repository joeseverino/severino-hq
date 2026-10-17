"""
Contact submission review screens.

Submissions are stored in Cloudflare D1 by the example.com contact form. These
views read the copy the controller last read (``inbox``) and never reach D1. A
review or a deletion is queued for the controller, which applies it through
the Cloudflare API connection; until it does, the message shows the change as
waiting. Every change is recorded in the HQ audit log.
"""

from urllib.parse import quote

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from hq.platform.application.contact_submissions import (
    ContactDeleteCommand,
    ContactReviewCommand,
    execute_contact_delete,
    execute_contact_review,
    find_submission,
    list_contact_submissions,
)
from hq.platform.application.pages import PageAction, page_context
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.ui import counted

from . import inbox
from .forms import STATUS_CHOICES, ContactReviewForm

VALID_STATUSES = {value for value, _ in STATUS_CHOICES}
QUEUED = "The controller applies it on its next run."


def reply_mailto(address: str, subject: str = "Re: your message") -> str:
    """A mailto URL whose address and subject cannot add headers.

    The address comes from a public form: CR and LF are stripped, and it and
    the subject are percent-encoded so `?`, `&` and `%` stay literal.
    """

    address = "".join(ch for ch in str(address or "") if ch not in "\r\n")
    subject = "".join(ch for ch in subject if ch not in "\r\n")
    return f"mailto:{quote(address, safe='@')}?subject={quote(subject, safe='')}"


def _waiting_note(current: inbox.Inbox) -> str:
    if current.refused:
        return f"The controller could not apply the last change: {current.refused}"
    if current.waiting:
        return f"{counted(current.waiting, 'change')} waiting for the controller."
    return ""


def contact_list(request):
    selected_status = request.GET.get("status", "").strip()
    if selected_status not in VALID_STATUSES:
        selected_status = ""
    q = request.GET.get("q", "").strip()
    current = inbox.current()
    submissions = list_contact_submissions(status=selected_status, query=q, limit=500)["items"]
    status_tabs = [
        {"value": value, "label": label, "count": current.counts.get(value, 0)} for value, label in STATUS_CHOICES
    ]

    return render(
        request,
        "contacts/contact_list.html",
        {
            "not_read": current.state == inbox.NOT_READ,
            "submissions": submissions,
            "status_tabs": status_tabs,
            "selected_status": selected_status,
            "q": q,
            "total_count": sum(current.counts.values()),
            "problem": current.problem,
            "observed_at": current.observed_at,
            "waiting_note": _waiting_note(current),
            **page_context(
                "Messages", "What people sent through the contact form on the site.", reads=contact_list.page_reads
            ),
        },
    )


contact_list.page_reads = ("cloudflare.contact_submission",)  # type: ignore[attr-defined]


def _review(request, pk: int, command: ContactReviewCommand) -> bool:
    """Queue one review; say why not when it is refused."""

    try:
        execute_contact_review(command, principal=web_principal(request.user), current_id=pk)
    except ValueError as exc:
        messages.error(request, str(exc))
        return False
    return True


def contact_detail(request, pk: int):
    submission = find_submission(pk)
    if not submission:
        messages.error(request, f"Message #{pk} was not found.")
        return redirect("contacts:list")

    if request.method == "POST":
        form = ContactReviewForm(request.POST)
        if form.is_valid():
            command = ContactReviewCommand(
                status=form.cleaned_data["status"],
                assigned_to=form.cleaned_data["assigned_to"],
                admin_notes=form.cleaned_data["admin_notes"],
            )
            if _review(request, pk, command):
                messages.success(request, f"Message #{pk} saved. {QUEUED}")
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
            "reply_href": reply,
            "form": form,
            **page_context(
                f"Message #{pk}",
                f"{submission.get('name', '')} · {submission.get('created_at', '')}",
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
    if status not in VALID_STATUSES:
        messages.error(request, f"Unknown status: {status!r}.")
        return redirect(_safe_next(request))

    submission = find_submission(pk)
    if not submission:
        messages.error(request, f"Message #{pk} was not found.")
        return redirect("contacts:list")
    command = ContactReviewCommand(
        status=status,
        assigned_to=submission.get("assigned_to") or "",
        admin_notes=submission.get("admin_notes") or "",
    )
    if _review(request, pk, command):
        messages.success(request, f"Message #{pk} marked {status}. {QUEUED}")
    return redirect(_safe_next(request))


def contact_delete(request, pk: int):
    submission = find_submission(pk)
    if not submission:
        messages.error(request, f"Message #{pk} was not found.")
        return redirect("contacts:list")

    if request.method == "POST":
        try:
            execute_contact_delete(
                ContactDeleteCommand(confirm=str(pk)),
                principal=web_principal(request.user),
                current_id=pk,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("contacts:detail", pk=pk)
        messages.success(request, f"Message #{pk} will be deleted. {QUEUED}")
        return redirect("contacts:list")

    return render(
        request,
        "contacts/contact_confirm_delete.html",
        {
            "submission": submission,
            "confirm": {
                "url": reverse("contacts:delete", args=[pk]),
                "label": f"Delete message #{pk}",
                "cancel_url": reverse("contacts:detail", args=[pk]),
            },
            **page_context(
                f"Delete message #{pk}?",
                f"{submission.get('created_at', '')} · {submission.get('name', '')} ({submission.get('email', '')})",
                trail=(
                    ("Messages", reverse("contacts:list")),
                    (f"Message #{pk}", reverse("contacts:detail", args=[pk])),
                ),
            ),
        },
    )
