from django import forms

from hq.domains.control_plane.provider_adapters.cloudflare import SubmissionStatus


def submission_state(submission: dict) -> SubmissionStatus | str:
    """A stored message's status as the member its pill is drawn from, unread
    when it has none; a word the table holds that HQ does not know stays a
    word, drawn in no tone."""

    status = str(submission.get("status") or SubmissionStatus.UNREAD)
    return SubmissionStatus(status) if status in SubmissionStatus.values else status


class ContactReviewForm(forms.Form):
    """The admin-editable fields on a contact submission, queued for the controller to write to D1."""

    status = forms.ChoiceField(choices=SubmissionStatus.choices)
    assigned_to = forms.CharField(required=False, max_length=120)
    admin_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
