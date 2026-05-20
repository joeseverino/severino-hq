from django import forms

# Mirrors the `status` values the contact_submissions table can hold.
STATUS_CHOICES = [
    ("unread", "Unread"),
    ("read", "Read"),
    ("replied", "Replied"),
    ("archived", "Archived"),
    ("spam", "Spam"),
]


class ContactReviewForm(forms.Form):
    """The admin-editable fields on a contact submission (written back to D1)."""

    status = forms.ChoiceField(choices=STATUS_CHOICES)
    assigned_to = forms.CharField(required=False, max_length=120)
    admin_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
