"""Form pieces every HQ surface can reuse.

These live in the host because what they solve is a property of the browser and
of Django, not of any one domain. A surface that accepts several files at once
should not have to rediscover that ``FileField`` binds exactly one upload.
"""

import base64
from typing import Any, override

from django import forms


class MultipleFileInput(forms.FileInput):
    """A file input that reports every selected file.

    Django gates multiple selection behind this flag rather than the ``multiple``
    attribute, because a widget that renders ``multiple`` while reading a single
    file is worse than one that refuses: it silently drops uploads. Setting the
    flag makes the widget read ``files.getlist`` and add the attribute itself.
    """

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A ``FileField`` that validates every selected file, not just one.

    ``FileField.clean`` keeps one file of a multiple input and drops the rest
    with no error. This runs the field's own validation over each upload, so
    the caller gets every file or the first failure.
    """

    widget = MultipleFileInput

    @override
    def clean(self, data: Any, initial: Any = None) -> list[Any]:
        # Bound before the comprehension: a zero-argument ``super()`` inside one
        # resolves against the comprehension's own scope, not this method's.
        clean_one = super().clean
        if isinstance(data, (list, tuple)):
            uploads = [item for item in data if item not in self.empty_values]
        else:
            uploads = [] if data in self.empty_values else [data]
        if not uploads:
            # Delegate the empty case so a required field raises the same message
            # every other field on the form would.
            clean_one(None, initial)
            return []
        return [clean_one(item, initial) for item in uploads]


class ChoiceCards(forms.RadioSelect):
    """One answer of a few, each a card that says what choosing it does
    (docs/Design.md, "A choice that has a consequence says it").

    ``described`` maps each value to its consequence, which the card's radio
    names as its description, so the answer's name stays its few words.
    """

    template_name = "django/forms/widgets/choice_cards.html"

    def __init__(self, *args, described: dict[str, str], **kwargs):
        super().__init__(*args, **kwargs)
        self.described = described

    @override
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        option["description"] = self.described.get(str(value), "")
        if option["description"] and (own := option["attrs"].get("id")):
            named = option["attrs"].get("aria-describedby", "")
            option["attrs"]["aria-describedby"] = f"{own}_what {named}".strip()
        return option


class SuffixedInput(forms.TextInput):
    """A line of words with its unit after it (a percent sign), drawn beside
    the box and never typed into it."""

    template_name = "django/forms/widgets/suffixed.html"

    def __init__(self, *args, suffix: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.suffix = suffix

    @override
    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["suffix"] = self.suffix
        return context


class LinesField(forms.Field):
    """A list edited as one item per line."""

    @override
    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return "\n".join(str(item) for item in value)
        return value


def file_as_sent(upload: Any) -> dict[str, str]:
    """An uploaded file as a command takes one in JSON: its name, its media
    type, and its bytes base64-encoded (``command_fields.Upload``)."""

    upload.seek(0)
    return {
        "name": upload.name or "",
        "content_type": upload.content_type or "",
        "content": base64.b64encode(upload.read()).decode("ascii"),
    }


class UploadField(forms.FileField):
    """A file a person picks, for a command field that takes a file as JSON."""

    @override
    def clean(self, data: Any, initial: Any = None) -> dict[str, str] | None:
        upload = super().clean(data, initial)
        return file_as_sent(upload) if upload else None
