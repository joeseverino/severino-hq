"""The catalog's forms: a form page, its lookups, filter bars, choices and drop zones."""

from typing import Any

from django import forms

from hq.platform.application.adoption import ONLY_READS, WriteGap
from hq.platform.core.part_examples.forms import TOGGLES, ExampleChoices, ExampleFiles, ExampleForm
from hq.platform.core.template_parts import Example
from hq.platform.core.tests.browser.selectors import SELECTORS

# Each form not drawn as one question, or drawn otherwise than its width
# calls for: its button on the control's line in a form of 30rem or more
# inside its padding, under the control in one narrower (forms.css).
_ONE_QUESTION = """({forms, question, control: controls, button: buttons}) => [...document.querySelectorAll(forms)].filter((form) => {
  if (!form.matches(question)) return true;
  const control = form.querySelector(controls).getBoundingClientRect();
  const button = form.querySelector(buttons).getBoundingClientRect();
  const style = getComputedStyle(form);
  const width = form.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const roomy = width >= 30 * parseFloat(getComputedStyle(document.documentElement).fontSize);
  return roomy ? !(button.top < control.bottom && button.bottom > control.top) : button.top < control.bottom;
}).map((form) => form.outerHTML.slice(0, 120))"""


_QUESTION_PARTS = {
    "forms": "main form",
    "question": SELECTORS["one_question"],
    "control": SELECTORS["question_control"],
    "button": SELECTORS["question_button"],
}


class _ExampleRange(forms.Form):
    """A form of pairs (``starts_pair``): each range's start and end on a row
    of their own, whatever columns the form has, and a field after them."""

    starts_on = forms.DateField(label="Example start", widget=forms.DateInput(attrs={"type": "date"}))
    ends_on = forms.DateField(
        label="Example end", required=False, widget=forms.DateInput(attrs={"type": "date"}), help_text="An example end."
    )
    starts_at = forms.TimeField(label="Example from", required=False, widget=forms.TimeInput(attrs={"type": "time"}))
    ends_at = forms.TimeField(label="Example to", required=False, widget=forms.TimeInput(attrs={"type": "time"}))
    place = forms.CharField(label="Example place", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # An option field.html reads, which Django's Field does not declare.
        fields: dict[str, Any] = dict(self.fields)
        for name in ("starts_on", "starts_at"):
            fields[name].starts_pair = True


class _NoFields(forms.Form):
    """A command that takes nothing."""


class _ExampleLookup(forms.Form):
    name = forms.CharField(label="Example hostname", required=False)


class _ExampleNaming(forms.Form):
    name = forms.CharField(
        label="Example group", help_text="An example of what a group is named for, long enough to wrap on a phone."
    )


ENTRIES = {
    # The form page's card (form_page.html) with fields and what saving does,
    # one with no fields, which is its closing row alone, and a form that asks
    # (partials/_form.html method="get"), one question on its button's line,
    # once more with its help and an error under them; a new option of either
    # comes with an entry here (test_form_page).
    "form/page": Example(
        '{% include "form_page.html#form_card" %}',
        {
            "form": ExampleForm(),
            "form_verb": "Add example",
            "cancel_url": "#",
            "form_note": "An example note on what saving does, long enough to wrap on a phone.",
        },
    ),
    "form/page-fieldless": Example(
        '{% include "form_page.html#form_card" %}', {"form": _NoFields(), "form_verb": "Run example", "cancel_url": "#"}
    ),
    "form/page-pairs": Example(
        '{% include "form_page.html#form_card" %}',
        {"form": _ExampleRange(), "form_verb": "Add example", "cancel_url": "#"},
    ),
    # A form whose connection cannot make the change (`gap`): its verb drawn
    # unusable, with what it needs beside it.
    "form/write-gap": Example(
        '{% section "Example record" %}{% include "partials/_form.html" with form=form verb="Save example" '
        'cancel_url="#" gap=gap note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {
            "form": ExampleForm(),
            "gap": WriteGap(
                "HQ only reads through Example connection example-main. A change through it also needs Example Write.",
                ONLY_READS,
                "Needs manages: 1 and Example Write",
                "#",
            ),
        },
    ),
    # A form whose answers each say what choosing them does (`ChoiceCards`), one
    # card a choice, and a number with its unit after the box
    # (`SuffixedInput`), sent with the number past its limit.
    "form/choice-cards": Example(
        '{% section "Example policy" %}{% include "partials/_form.html" with form=form verb="Save example" '
        'cancel_url="" note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {"form": ExampleChoices(data={"answer": "refuse", "share": "150"})},
    ),
    "form/lookup": Example(
        '{% section "Example lookup" %}{% include "partials/_form.html" with method="get" form=form verb="Look up" '
        'cancel_url="" note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {"form": _ExampleLookup()},
        premise=(_ONE_QUESTION, _QUESTION_PARTS),
    ),
    "form/lookup-help": Example(
        '{% section "Example group" %}{% include "partials/_form.html" with form=form verb="Add example group" '
        'cancel_url="" note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {"form": _ExampleNaming(data={})},
        premise=(_ONE_QUESTION, _QUESTION_PARTS),
    ),
    # The filter bar ({% filter_bar %}) narrowed, with its way back, its
    # count and a fact about what shows; holding a part back, with the one
    # link that shows it, its choices folded under the id it is given; with one choice and nothing narrowed; and with
    # labeled choices asking another page, its verb its own. A new option of
    # the tag comes with an entry here (test_part_catalog).
    "form/filter-bar": Example(
        '{% filter_bar search="Filter example records" query="example" keep=kept autofocus=True filtered=True '
        'clear="#" shown=3 total=12 %}'
        "{% choices %}"
        '{% select "state" states chosen="on" label="Example state" every="example state" %}'
        '{% select "kind" kinds label="Example kind" every="example kind" %}'
        "{% summary %}1 needs a look.{% endfilter_bar %}",
        {
            "states": (("on", "Online"), ("off", "Offline")),
            "kinds": (("host", "Example host"),),
            "kept": {"about": "example.host"},
        },
    ),
    "form/filter-bar-held": Example(
        '{% filter_bar search="Find an example" id="example-held" shown=93 total=170 held=held more="#" %}{% choices %}'
        '{% select "view" views label="Example view" %}{% endfilter_bar %}',
        {
            "views": (("", "Example main view"), ("everything", "Everything")),
            "held": ("30 example readings", "20 example lookups", "27 example records with no links"),
        },
    ),
    "form/filter-bar-one": Example(
        '{% filter_bar %}{% select "year" years chosen=2026 label="Example year" submit_on_change=True %}'
        "{% endfilter_bar %}",
        {"years": (2026, 2025)},
    ),
    "form/filter-bar-labeled": Example(
        '{% filter_bar verb="Check" action="#" css="example" data_example="example" %}'
        '{% control_label "From" control="example-from" %}'
        '{% select "from" from_hosts id="example-from" %}'
        '{% control_label "To" control="example-to" %}'
        '{% select "to" to_hosts id="example-to" %}'
        "{% endfilter_bar %}",
        {"from_hosts": ("example-host-01",), "to_hosts": ("example-host-02",)},
    ),
    # A set of toggles in a filter bar, some on and some off, enough of them
    # to wrap in a narrow bar, and a control a script shows only once it has
    # something to act on.
    "form/filter-bar-toggles": Example(
        '{% filter_bar search="Find an example" shown=12 total=12 %}{% choices %}'
        '{% include "partials/_toggle_set.html" with legend="Example kinds" set="kind" name="kind" toggles=toggles only %}'
        '<button class="btn ghost compact" type="button" hidden>Example clear</button>'
        "{% endfilter_bar %}",
        {"toggles": TOGGLES},
    ),
    # A list's table toolbar with several toggles and a sort, as a list page
    # draws it: its controls wrap in a narrow bar, and the button that sends
    # them stays beside the last of them.
    "form/filter-bar-sorted": Example(
        '{% include "partials/_table_toolbar.html" %}',
        {
            "table": {
                "query": "",
                "search_placeholder": "Search example projects",
                "filters": (),
                "toggles": tuple(
                    {"name": f"example_{number}", "label": f"Example toggle {number}", "selected": number == 1}
                    for number in range(1, 5)
                ),
                "sorts": ({"value": "name", "label": "Name"}, {"value": "updated", "label": "Last updated"}),
                "sort_choices": (("name", "Name"), ("updated", "Last updated")),
                "selected_sort": "name",
                "active_count": 0,
            }
        },
    ),
    # A file field that takes a drop: as it is first drawn, and sent with
    # nothing in it.
    "form/dropzone": Example(
        '{% section "Example import" %}{% include "partials/_dropzone.html" with field=form.exports '
        'title="Drop example exports here" hint="CSV or JSON, several at once" %}{% endsection %}',
        {"form": ExampleFiles()},
    ),
    "form/dropzone-errors": Example(
        '{% section "Example import 2" %}{% include "partials/_dropzone.html" with field=form.exports %}{% endsection %}',
        {"form": ExampleFiles(data={})},
    ),
}
