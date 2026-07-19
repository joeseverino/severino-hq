"""
Contact submission review screens.

Submissions are stored in Cloudflare D1 by the jseverino.com contact form.
These views read and write that table over the D1 HTTP API — there is no
local model. Review edits (status / assignee / notes) are written back to D1
and recorded in the HQ audit log.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.audit import record_event
from core.models import AuditLog

from .d1 import (
    D1Error,
    delete_submission,
    get_submission,
    list_submissions,
    set_status,
    status_counts,
    update_submission,
)
from .forms import STATUS_CHOICES, ContactReviewForm

VALID_STATUSES = {value for value, _ in STATUS_CHOICES}


@login_required
def contact_list(request):
    selected_status = request.GET.get("status", "").strip()
    q = request.GET.get("q", "").strip()
    error = ""
    submissions: list[dict] = []
    counts: dict[str, int] = {}

    try:
        submissions = list_submissions(status=selected_status, q=q)
        counts = status_counts()
    except D1Error as exc:
        error = str(exc)

    status_tabs = [
        {"value": value, "label": label, "count": counts.get(value, 0)}
        for value, label in STATUS_CHOICES
    ]

    return render(
        request,
        "contacts/contact_list.html",
        {
            "submissions": submissions,
            "status_tabs": status_tabs,
            "selected_status": selected_status,
            "q": q,
            "total_count": sum(counts.values()),
            "error": error,
        },
    )


@login_required
def contact_detail(request, pk: int):
    try:
        submission = get_submission(pk)
    except D1Error as exc:
        messages.error(request, str(exc))
        return redirect("contacts:list")

    if not submission:
        messages.error(request, f"Contact submission #{pk} was not found.")
        return redirect("contacts:list")

    if request.method == "POST":
        form = ContactReviewForm(request.POST)
        if form.is_valid():
            try:
                update_submission(
                    pk,
                    form.cleaned_data["status"],
                    form.cleaned_data["assigned_to"],
                    form.cleaned_data["admin_notes"],
                )
            except D1Error as exc:
                messages.error(request, str(exc))
                return redirect("contacts:detail", pk=pk)

            record_event(
                action=AuditLog.Action.UPDATED,
                type_label="Contact submission",
                message=(
                    f"Reviewed contact submission #{pk} "
                    f"({submission.get('email', '')}) — status "
                    f"{form.cleaned_data['status']}"
                ),
                metadata={"id": pk, "status": form.cleaned_data["status"]},
            )
            messages.success(request, f"Submission #{pk} updated.")
            return redirect("contacts:detail", pk=pk)
    else:
        form = ContactReviewForm(
            initial={
                "status": submission.get("status") or "unread",
                "assigned_to": submission.get("assigned_to") or "",
                "admin_notes": submission.get("admin_notes") or "",
            }
        )

    return render(
        request,
        "contacts/contact_detail.html",
        {"submission": submission, "form": form},
    )


def _safe_next(request) -> str:
    """Only follow same-host, same-app redirect targets; else back to the list."""
    target = request.POST.get("next", "")
    if target.startswith(reverse("contacts:list")) and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return reverse("contacts:list")


@login_required
@require_POST
def contact_set_status(request, pk: int):
    status = request.POST.get("status", "")
    if status not in VALID_STATUSES:
        messages.error(request, f"Unknown status: {status!r}.")
        return redirect(_safe_next(request))

    try:
        submission = get_submission(pk)
        if not submission:
            messages.error(request, f"Contact submission #{pk} was not found.")
            return redirect("contacts:list")
        set_status(pk, status)
    except D1Error as exc:
        messages.error(request, str(exc))
        return redirect(_safe_next(request))

    record_event(
        action=AuditLog.Action.UPDATED,
        type_label="Contact submission",
        message=(
            f"Marked contact submission #{pk} "
            f"({submission.get('email', '')}) {status}"
        ),
        metadata={"id": pk, "status": status},
    )
    messages.success(request, f"Submission #{pk} marked {status}.")
    return redirect(_safe_next(request))


@login_required
def contact_delete(request, pk: int):
    try:
        submission = get_submission(pk)
    except D1Error as exc:
        messages.error(request, str(exc))
        return redirect("contacts:list")

    if not submission:
        messages.error(request, f"Contact submission #{pk} was not found.")
        return redirect("contacts:list")

    if request.method == "POST":
        try:
            delete_submission(pk)
        except D1Error as exc:
            messages.error(request, str(exc))
            return redirect("contacts:detail", pk=pk)

        record_event(
            action=AuditLog.Action.DELETED,
            type_label="Contact submission",
            message=(
                f"Deleted contact submission #{pk} "
                f"({submission.get('email', '')})"
            ),
            metadata={"id": pk},
        )
        messages.success(request, f"Submission #{pk} deleted.")
        return redirect("contacts:list")

    return render(
        request,
        "contacts/contact_confirm_delete.html",
        {"submission": submission},
    )
