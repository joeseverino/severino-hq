from typing import override

from django import forms

from .models import Entry

WEEKDAYS = (
    ("0", "Mon"),
    ("1", "Tue"),
    ("2", "Wed"),
    ("3", "Thu"),
    ("4", "Fri"),
    ("5", "Sat"),
    ("6", "Sun"),
)


class EntryForm(forms.ModelForm):
    """An event, as a person fills it in: a day, then the rest if it applies."""

    weekdays = forms.MultipleChoiceField(
        choices=WEEKDAYS,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="On",
        help_text="For an event that repeats weekly. Leave blank to repeat on the day it starts.",
    )

    class Meta:
        model = Entry
        fields = [
            "title",
            "starts_on",
            "starts_at",
            "ends_at",
            "ends_on",
            "location",
            "about",
            "repeat",
            "interval",
            "weekdays",
            "repeat_until",
            "notes",
        ]
        labels = {
            "starts_on": "Day",
            "starts_at": "From",
            "ends_on": "Until day",
            "ends_at": "To",
            "interval": "Every",
            "repeat_until": "Stop repeating after",
            "about": "About",
        }
        help_texts = {
            "starts_at": "Leave the times blank for all day.",
            "ends_on": "For something over several days.",
            "interval": "1 for every one, 2 for every other one.",
            "about": "Something HQ has a page for. The event is listed on that page.",
        }
        widgets = {
            "starts_on": forms.DateInput(attrs={"type": "date"}),
            "ends_on": forms.DateInput(attrs={"type": "date"}),
            "repeat_until": forms.DateInput(attrs={"type": "date"}),
            "starts_at": forms.TimeInput(attrs={"type": "time"}),
            "ends_at": forms.TimeInput(attrs={"type": "time"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # What the entry is and what to remember about it take the whole row;
        # the when and the repeat read across as one line each.
        for name in ("title", "notes"):
            self.fields[name].wide = True
        self.fields["title"].widget.attrs.update(autofocus=True, placeholder="What is it?")
        if self.instance.pk:
            self.initial["weekdays"] = [str(day) for day in self.instance.weekday_numbers]

    def clean_weekdays(self) -> tuple[int, ...]:
        return tuple(int(day) for day in self.cleaned_data["weekdays"])

    @override
    def _post_clean(self) -> None:
        # The model validates the whole entry; it stores weekdays as text.
        weekdays = self.cleaned_data.get("weekdays", ())
        self.cleaned_data["weekdays"] = ",".join(str(day) for day in weekdays)
        super()._post_clean()
        self.cleaned_data["weekdays"] = weekdays
