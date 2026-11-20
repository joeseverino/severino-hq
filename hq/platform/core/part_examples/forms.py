"""The form parts' examples: a field of each kind, a form, its errors and its actions."""

from typing import override

from django import forms

from hq.platform.application.forms import ChoiceCards, SuffixedInput
from hq.platform.core.template_parts import Example


class ExampleForm(forms.Form):
    name = forms.CharField(label="Example name", help_text="An example of what the name is for.")
    notes = forms.CharField(label="Example notes", required=False, widget=forms.Textarea)


_EXAMPLE_ANSWERS = (
    ("refuse", "Refuse it", "Every example nobody listed is refused, which is what the example policy needs to work."),
    ("mark", "Mark it as suspicious", "Receivers usually accept it and flag it."),
    ("allow", "Allow it", "Every example passes."),
)


class ExampleChoices(forms.Form):
    answer = forms.ChoiceField(
        label="Every other example",
        choices=[(value, label) for value, label, _ in _EXAMPLE_ANSWERS],
        widget=ChoiceCards(described={value: what for value, _, what in _EXAMPLE_ANSWERS}),
        help_text="What happens to an example the list above leaves out.",
    )
    share = forms.IntegerField(
        label="Apply it to", min_value=0, max_value=100, widget=SuffixedInput(suffix="%"), help_text="Of every example."
    )


class ExampleFiles(forms.Form):
    exports = forms.FileField(label="Example exports", help_text="An example of the files it takes.")


class _ExampleWhole(forms.Form):
    """A form refused as a whole, its error above its fields."""

    name = forms.CharField(label="Example name", required=False)

    @override
    def clean(self):
        raise forms.ValidationError("An example of what was wrong with the form as a whole.")


# A set of toggles, some on and some off, enough of them to wrap in a narrow bar.
TOGGLES = [
    {"value": f"example-{number}", "label": f"Example kind {number}", "checked": number % 3 != 0}
    for number in range(1, 7)
]

FIELD = Example("{{ field.as_field_group }}", context=lambda: {"field": ExampleForm()["name"]})
CHOICE_CARDS = Example("{{ field.as_field_group }}", context=lambda: {"field": ExampleChoices()["answer"]})
SUFFIXED = Example("{{ field.as_field_group }}", context=lambda: {"field": ExampleChoices()["share"]})
DROPZONE = Example(
    context=lambda: {
        "field": ExampleFiles()["exports"],
        "title": "Drop example exports here",
        "hint": "CSV or JSON, several at once",
    }
)
ROW_FIELD = Example(context=lambda: {"field": ExampleForm()["name"]})
FORM = Example(
    context=lambda: {
        "form": ExampleForm(),
        "verb": "Save example",
        "cancel_url": "#",
        "note": "",
        "choices": "",
        "choice_field": "",
        "bar": "",
    }
)
FORM_ACTIONS = Example(
    context={"verb": "Save example", "cancel_url": "#", "choice_field": "", "note": "Saving changes the example."}
)
FILTER_BAR = Example(
    '{% filter_bar search="Filter by name" query=q filtered=narrowed clear=every_url %}'
    '{% choices %}{% select "kind" kinds label="Kind" every="kind" %}{% endfilter_bar %}',
    context={"kinds": (("host", "Example host"),), "every_url": "#"},
)
FORM_ERRORS = Example(context=lambda: {"form": _ExampleWhole(data={})})
TOGGLE_SET = Example(context={"legend": "Example kinds", "set": "kind", "name": "kind", "toggles": TOGGLES})
