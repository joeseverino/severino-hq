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

from core.audit import record_event
from core.models import AuditLog

from .d1 import D1Error, query
from .forms import STATUS_CHOICES, ContactReviewForm

LIST_COLUMNS = "id, created_at, name, email, status, country"


@login_required
def contact_list(request):
    selected_status = request.GET.get("status", "").strip()
    error = ""
    submissions: list[dict] = []

    try:
        if selected_status:
            submissions = query(
                f"SELECT {LIST_COLUMNS} FROM contact_submissions "
                f"WHERE status = ? ORDER BY id DESC LIMIT 500",
                [selected_status],
            )
        else:
            submissions = query(
                f"SELECT {LIST_COLUMNS} FROM contact_submissions "
                f"ORDER BY id DESC LIMIT 500"
            )
    except D1Error as exc:
        error = str(exc)

    return render(
        request,
        "contacts/contact_list.html",
        {
            "submissions": submissions,
            "status_choices": STATUS_CHOICES,
            "selected_status": selected_status,
            "error": error,
        },
    )


@login_required
def contact_detail(request, pk: int):
    try:
        rows = query("SELECT * FROM contact_submissions WHERE id = ?", [pk])
    except D1Error as exc:
        messages.error(request, str(exc))
        return redirect("contacts:list")

    if not rows:
        messages.error(request, f"Contact submission #{pk} was not found.")
        return redirect("contacts:list")

    submission = rows[0]

    if request.method == "POST":
        form = ContactReviewForm(request.POST)
        if form.is_valid():
            try:
                query(
                    "UPDATE contact_submissions SET status = ?, assigned_to = ?, "
                    "admin_notes = ?, updated_at = datetime('now') WHERE id = ?",
                    [
                        form.cleaned_data["status"],
                        form.cleaned_data["assigned_to"],
                        form.cleaned_data["admin_notes"],
                        pk,
                    ],
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
