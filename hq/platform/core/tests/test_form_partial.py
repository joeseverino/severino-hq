"""A form that writes is one partial, ``partials/_form.html``."""

from django import forms
from django.template import engines
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import all_elements, elements, select, with_class
from hq.platform.application.web_writes import RETRY_KEY_FIELD

FORM = "partials/_form.html"


class ExampleForm(forms.Form):
    name = forms.CharField()
    notes = forms.CharField(required=False, widget=forms.Textarea)

    def clean(self):
        raise forms.ValidationError("Example names are taken.")


class ExampleUpload(forms.Form):
    file = forms.FileField()


class FormPartialTests(SimpleTestCase):
    def test_it_draws_the_token_the_errors_the_fields_and_the_closing_row_in_one_form(self):
        form = ExampleForm(data={"name": "example"})
        form.is_valid()

        html = render_to_string(FORM, {"form": form, "verb": "Save example", "cancel_url": "/example/"})

        (drawn,) = elements(html, "form")
        self.assertEqual(drawn.attrs["method"], "post")
        self.assertNotIn("action", drawn.attrs)
        self.assertNotIn("enctype", drawn.attrs)
        self.assertIn("data-submit-busy", drawn.attrs)
        inside = [e for e in elements(html, "input") + elements(html, "textarea") if drawn in e.ancestors]
        self.assertEqual({e.attrs.get("name") for e in inside}, {"name", "notes"})
        self.assertIn(RETRY_KEY_FIELD, html)
        (error,) = select(html, "role", "alert")
        self.assertEqual(error.words, "Example names are taken.")
        (row,) = with_class(html, "form-actions")
        self.assertIn(drawn, row.ancestors)
        (button,) = elements(html, "button")
        self.assertEqual((button.words, button.attrs["type"]), ("Save example", "submit"))
        self.assertEqual([a.attrs["href"] for a in elements(html, "a")], ["/example/"])
        self.assertEqual(len(select(html, "data-submit-status")), 1)
        self.assertNotIn("save-bar", row.classes)

    def test_it_posts_where_it_is_told_and_takes_a_file_when_its_form_does(self):
        html = render_to_string(FORM, {"form": ExampleUpload(), "verb": "Upload", "action": "/example/upload/"})

        (drawn,) = elements(html, "form")
        self.assertEqual(drawn.attrs["action"], "/example/upload/")
        self.assertEqual(drawn.attrs["enctype"], "multipart/form-data")

    def test_a_form_that_asks_sends_its_fields_as_a_query_with_no_write_token(self):
        html = render_to_string(
            FORM, {"form": ExampleUpload(), "verb": "Look up", "action": "/example/lookup/", "method": "get"}
        )

        (drawn,) = elements(html, "form")
        self.assertEqual((drawn.attrs["method"], drawn.attrs["action"]), ("get", "/example/lookup/"))
        self.assertNotIn("enctype", drawn.attrs)
        self.assertNotIn(RETRY_KEY_FIELD, html)
        (row,) = with_class(html, "form-actions")
        self.assertIn(drawn, row.ancestors)
        (button,) = elements(html, "button")
        self.assertEqual(button.words, "Look up")

    def test_a_page_draws_its_own_fields_inside_it(self):
        html = render_to_string(
            FORM,
            {
                "form": ExampleUpload(),
                "verb": "Upload",
                "fields": "partials/_dropzone.html",
                "field": ExampleUpload()["file"],
            },
        )

        (drawn,) = elements(html, "form")
        (upload,) = [e for e in elements(html, "input") if e.attrs.get("type") == "file"]
        self.assertIn(drawn, upload.ancestors)
        self.assertEqual(len(with_class(html, "field")), 1)

    def test_a_long_form_pins_its_closing_row(self):
        html = render_to_string(FORM, {"form": ExampleForm(), "verb": "Save", "bar": True})

        (row,) = with_class(html, "form-actions")
        self.assertIn("save-bar", row.classes)


class ExampleRowsForm(forms.Form):
    """A field for each row of a table, named by its row, as a page's form declares them."""

    action = forms.CharField(widget=forms.HiddenInput, initial="assign")

    def __init__(self, *args, rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        for key, label in rows:
            self.fields[f"group-{key}"] = forms.ChoiceField(
                label=f"Group for {label}", choices=[("", "No group"), ("1", "Everyday")], required=False
            )


ROWS_TABLE = (
    '{% table_region "Accounts" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Account" "identifier" %}{% table_heading "Group" "control" %}</tr></thead><tbody>'
    "{% for name, field in rows %}<tr><td><strong>{{ name }}</strong></td>"
    '<td>{% include "partials/_row_field.html" with field=field only %}</td></tr>{% endfor %}'
    "</tbody></table>{% endtable_region %}"
)


class FormTableTests(SimpleTestCase):
    """A form whose fields stand one to a row of a table (`table_body`)."""

    def draw(self, form):
        rows = [("Example checking", form["group-1"]), ("Example savings", form["group-2"])]
        table = engines["django"].engine.from_string(ROWS_TABLE)
        context = {"form": form, "table_body": table, "rows": rows, "verb": "Save groups", "bar": True}
        return render_to_string(FORM, context)

    def test_the_table_is_the_forms_body_with_its_closing_row_under_it(self):
        html = self.draw(ExampleRowsForm(rows=[(1, "Example checking"), (2, "Example savings")]))

        (drawn,) = elements(html, "form")
        # It lays out no fields of its own, so the table reaches the frame's sides.
        self.assertNotIn("form", drawn.classes)
        (region,) = with_class(html, "table-scroll")
        self.assertIn(drawn, region.ancestors)
        order = all_elements(html)
        (row,) = with_class(html, "form-actions")
        self.assertIn(drawn, row.ancestors)
        self.assertGreater(order.index(row), order.index(region))
        self.assertIn("save-bar", row.classes)
        self.assertIn(RETRY_KEY_FIELD, html)
        hidden = [e for e in elements(html, "input") if e.attrs.get("type") == "hidden" and e.attrs["name"] == "action"]
        self.assertEqual([(e.attrs["value"], drawn in e.ancestors) for e in hidden], [("assign", True)])

    def test_each_rows_control_has_its_own_name_and_id_and_is_named_by_its_label(self):
        html = self.draw(ExampleRowsForm(rows=[(1, "Example checking"), (2, "Example savings")]))

        (region,) = with_class(html, "table-scroll")
        controls = elements(html, "select")
        self.assertEqual([e.attrs["name"] for e in controls], ["group-1", "group-2"])
        self.assertTrue(all(region in e.ancestors for e in controls))
        ids = [e.attrs["id"] for e in controls]
        self.assertEqual(len(set(ids)), 2)
        labels = {e.attrs["for"]: e for e in elements(html, "label")}
        self.assertEqual(sorted(labels), sorted(ids))
        self.assertEqual(labels[ids[0]].words, "Group for Example checking")
        self.assertTrue(all("visually-hidden" in label.classes for label in labels.values()))

    def test_a_rows_error_stands_under_its_control_and_the_control_names_it(self):
        form = ExampleRowsForm(data={"action": "assign", "group-1": "9"}, rows=[(1, "Example checking"), (2, "")])
        form.is_valid()

        html = self.draw(form)

        (control, _) = elements(html, "select")
        (error,) = with_class(html, "errors")
        (cell,) = [e for e in error.ancestors if e.tag == "td"]
        self.assertIn(cell, control.ancestors)
        self.assertIn(error.attrs["id"], control.attrs["aria-describedby"].split())
        self.assertIn("has-errors", error.ancestors[-1].classes)
