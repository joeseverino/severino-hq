"""Form pieces every HQ surface can reuse.

These live in the host because what they solve is a property of the browser and
of Django, not of any one domain. A surface that accepts several files at once
should not have to rediscover that ``FileField`` binds exactly one upload.
"""

import base64
from typing import Any, ClassVar, override

from django import forms
from django.utils.datastructures import MultiValueDict

lazy from hq.platform.core.held_values import format_html, format_html_join
lazy from hq.platform.core.templatetags.value_tags import breakable

from .ui import button_classes


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
    (its widget's Part in ``included_parts``).

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


class ListWidget(forms.Widget):
    """One input per item, with a row to add and a control to remove.

    In a textarea nothing says where one item ends, and editing the middle of
    eight lines is text editing rather than a choice. Rows post under one name
    and are read back with `getlist`, so the field receives a list and no parsing
    rule lives in two places. Without scripting, rows still edit and the spare
    row still adds; only adding and removing more needs JavaScript.
    """

    # Read by a form that refuses a field posted twice: this one posts a row each.
    allow_multiple_selected = True

    # ``{value: note}`` for values HQ can see for itself. A machine's addresses
    # are the case this exists for: half of them are the only record there is
    # (nothing reports a printer's address) and half repeat a reading from the
    # tailnet. Presented identically, the field invites somebody to correct HQ
    # about something HQ is watching, and gives no way to tell which is which.
    notes: ClassVar[dict[str, str]] = {}

    @override
    def value_from_datadict(self, data, files, name):
        if isinstance(data, MultiValueDict):
            return [item for item in data.getlist(name) if str(item).strip()]
        return data.get(name)

    @override
    def format_value(self, value):
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        if value in (None, ""):
            return []
        return [line.strip() for line in str(value).splitlines() if line.strip()]

    def _row(self, name: str, item: str, named: str):
        """One value, editable unless HQ is the one that found it.

        A value carrying a note is one a sweep reports, a fact HQ holds whether
        or not this field does, so it cannot be removed here. It posts as a
        hidden input, so a save keeps what it did not ask about.
        """

        note = self.notes.get(item, "")
        if note:
            return format_html(
                '<div class="name-list-row name-list-row-observed">'
                '<input type="hidden" name="{}" value="{}">'
                '<span class="name-list-observed">{}</span>'
                '<span class="name-list-note">{}</span>'
                "</div>",
                name,
                item,
                breakable(item),
                breakable(note),
            )
        return format_html(
            '<div class="name-list-row">'
            '<input type="text" name="{}" value="{}" spellcheck="false"'
            ' autocapitalize="off" autocorrect="off"{}>'
            '<button type="button" class="{}" data-name-list-remove'
            ' aria-label="Remove {}">Remove</button>'
            "</div>",
            name,
            item,
            named,
            button_classes("ghost"),
            item,
        )

    @override
    def render(self, name, value, attrs=None, renderer=None):
        # What HQ found first, what only this field records underneath it. The
        # observed one is the address the machine actually answers at on the
        # network everything reaches it over; the typed ones are the exceptions
        # nothing reports. Sorted so read-only rows never sit between inputs
        # and the blank row for adding one stays with the rest.
        values = sorted(self.format_value(value), key=lambda item: item not in self.notes)
        # Each row is named by the field's label (templates/django/forms/field.html
        # gives it the id) and described by what the field describes itself with.
        own = (attrs or {}).get("id")
        named = format_html(
            "{}{}",
            format_html(' aria-labelledby="{}_label"', own) if own else "",
            format_html(' aria-describedby="{}"', attrs["aria-describedby"])
            if attrs and attrs.get("aria-describedby")
            else "",
        )
        rows = format_html_join("", "{}", ((self._row(name, item, named),) for item in values))
        # Always one empty row, so adding an item needs no script and no
        # thinking about where the cursor goes.
        blank = format_html(
            '<div class="name-list-row">'
            '<input type="text" name="{}" value="" spellcheck="false"'
            ' autocapitalize="off" autocorrect="off"{}>'
            "</div>",
            name,
            named,
        )
        # A row added by the script takes its Remove button from the template,
        # so the button primitive's markup is drawn here alone.
        return format_html(
            '<div class="name-list" data-name-list>{}{}'
            '<button type="button" class="{} name-list-add" data-name-list-add>'
            "Add another</button>"
            '<template data-name-list-remover><button type="button" class="{}" data-name-list-remove>'
            "Remove</button></template></div>",
            rows,
            blank,
            button_classes(),
            button_classes("ghost"),
        )


# How a typed row is read as its item, and what a row that is not one is told.
_ITEMS = {
    "integer": (int, "Enter a whole number in each row."),
    "number": (float, "Enter a number in each row."),
    "boolean": (lambda item: {"true": True, "false": False}[item.casefold()], "Enter true or false in each row."),
}


class ListField(forms.Field):
    """A list of JSON primitives of one type, an item to a row."""

    widget = ListWidget

    def __init__(self, *args: Any, item_type: str = "string", **kwargs: Any) -> None:
        self.item_type = item_type
        super().__init__(*args, **kwargs)

    @override
    def to_python(self, value: Any) -> list[Any]:
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value]
        else:
            # Commas as well as newlines: a value written as text is usually
            # arriving from somewhere that comma-separates it, and silently
            # keeping "a.example.com, b.example.com" as one item would be a
            # bad surprise.
            items = (value or "").replace(",", "\n").splitlines()
        items = [item.strip() for item in items if item.strip()]
        if self.item_type not in _ITEMS:
            return items
        read, refusal = _ITEMS[self.item_type]
        try:
            return [read(item) for item in items]
        except (KeyError, ValueError) as exc:
            raise forms.ValidationError(refusal) from exc

    @override
    def validate(self, value: list[Any]) -> None:
        if self.required and not value:
            raise forms.ValidationError(self.error_messages["required"], code="required")


class ReadAt(forms.CharField):
    """When the record a form edits was read, never typed: sent back as the
    call's ``expected_updated_at``, so a save over a change made since is refused."""

    def __init__(self) -> None:
        super().__init__(required=False, widget=forms.HiddenInput())


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
    """A file a person picks, for a command field that takes a file as JSON.

    A form opened on a record holding a file names it as its initial, which
    the control shows and links (its ``url``), and which keeps the field
    filled: none is sent unless another is picked.
    """

    @override
    def clean(self, data: Any, initial: Any = None) -> dict[str, str] | None:
        upload = super().clean(data, initial)
        return file_as_sent(upload) if upload and upload is not initial else None
