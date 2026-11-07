"""A row's editor opens under its row (docs/Design.md, A row's editor opens under it)."""

from collections import Counter

from django import forms
from django.template import RequestContext, Template, TemplateSyntaxError
from django.test import RequestFactory, SimpleTestCase

from hq.platform.application.tests.markup import all_elements

EDITED = (
    '{% table_region "Example runs" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Run" "identifier" gutter=True %}{% table_heading "Miles" "numeric" %}'
    '{% table_heading "Distance check" "control" %}</tr></thead><tbody>'
    '{% for key, miles in rows %}<tr data-probe="{{ key }}"><td><strong>{{ key }}</strong></td><td>{{ miles }}</td>'
    '<td>{% row_editor form key label="Correct distance" action="/example/distance/" verb="Save distance"'
    " distance_miles=miles %}</td></tr>{% endfor %}"
    "</tbody></table>{% endtable_region %}"
)


class ExampleDistanceForm(forms.Form):
    distance_miles = forms.DecimalField(label="Miles", min_value=0)
    reason = forms.CharField(required=False)


def draw(address, form=None, source=EDITED):
    request = RequestFactory().get(address)
    context = {"form": form or ExampleDistanceForm(), "rows": [("example-01", "3.10"), ("example-02", "5.00")]}
    return Template(source).render(RequestContext(request, context))


def tagged(html, tag):
    """Every ``tag`` element, the line beneath each row included."""

    return [each for each in all_elements(html) if each.tag == tag]


def rows_of(found):
    return [each for each in found if each.tag == "tr" and any(up.tag == "tbody" for up in each.ancestors)]


class RowEditorTests(SimpleTestCase):
    def test_a_closed_row_links_to_the_page_with_its_editor_open_and_draws_no_form(self):
        html = draw("/example/runs/?q=run")

        self.assertEqual(tagged(html, "form"), [])
        ways_in = [each for each in tagged(html, "a") if "data-row-edit" in each.attrs]
        self.assertEqual(
            [(each.attrs["href"], each.attrs["aria-expanded"], each.words) for each in ways_in],
            [
                ("/example/runs/?q=run&edit=example-01#row-example-01", "false", "Correct distance"),
                ("/example/runs/?q=run&edit=example-02#row-example-02", "false", "Correct distance"),
            ],
        )
        # With script each is answered in the table's region, found by its id in the answer.
        (region,) = [each for each in all_elements(html) if "table-scroll" in each.classes]
        self.assertEqual((region.attrs["id"], region.attrs["data-fragment"]), ("table-example-runs", ""))
        self.assertTrue(all(each.attrs["data-fragment-target"] == "" for each in ways_in))

    def test_the_open_rows_editor_stands_in_the_line_beneath_it_with_the_rows_values(self):
        html = draw("/example/runs/?q=run&edit=example-02")

        found = all_elements(html)
        rows = rows_of(found)
        self.assertEqual(
            [row.attrs.get("data-probe", row.attrs.get("class")) for row in rows],
            ["example-01", "example-02", "row-detail"],
        )
        opened, line = rows[1], rows[2]
        self.assertIn("is-expanded", opened.classes)
        self.assertNotIn("is-expanded", rows[0].classes)
        (form,) = tagged(html, "form")
        self.assertIn(line, form.ancestors)
        self.assertEqual(form.attrs["action"], "/example/distance/")
        (toggle,) = [each for each in found if opened in each.ancestors and "data-row-toggle" in each.attrs]
        self.assertEqual(toggle.attrs["aria-expanded"], "true")
        fields = {each.attrs["name"]: each for each in tagged(html, "input") if form in each.ancestors}
        self.assertEqual(fields["distance_miles"].attrs["value"], "5.00")
        self.assertEqual(fields["next"].attrs["value"], "/example/runs/?q=run")
        (cancel,) = [each for each in tagged(html, "a") if form in each.ancestors]
        self.assertEqual((cancel.words, cancel.attrs["href"]), ("Cancel", "/example/runs/?q=run"))
        self.assertEqual(cancel.attrs["data-fragment-target"], "")
        self.assertEqual(form.attrs["data-fragment-target"], "")
        (way_in,) = [each for each in found if opened in each.ancestors and "data-row-edit" in each.attrs]
        self.assertEqual((way_in.attrs["href"], way_in.attrs["aria-expanded"]), ("/example/runs/?q=run", "true"))

    def test_the_editors_ids_are_its_rows_own_and_none_is_drawn_twice(self):
        html = draw("/example/runs/?edit=example-01")

        ids = Counter(each.attrs["id"] for each in all_elements(html) if "id" in each.attrs)
        self.assertEqual([name for name, count in ids.items() if count > 1], [])
        (field,) = [each for each in tagged(html, "input") if each.attrs.get("name") == "distance_miles"]
        self.assertIn("example-01", field.attrs["id"])
        (label,) = [each for each in tagged(html, "label") if each.attrs.get("for") == field.attrs["id"]]
        self.assertEqual(label.words, "Miles *")

    def test_a_form_sent_back_is_drawn_as_it_was_sent(self):
        sent = ExampleDistanceForm(data={"distance_miles": "-1", "reason": "example"})
        sent.is_valid()

        html = draw("/example/runs/?edit=example-01", form=sent)

        fields = {each.attrs["name"]: each for each in tagged(html, "input")}
        self.assertEqual((fields["distance_miles"].attrs["value"], fields["reason"].attrs["value"]), ("-1", "example"))
        (error,) = [each for each in all_elements(html) if "errors" in each.classes]
        self.assertIn(error.attrs["id"], fields["distance_miles"].attrs["aria-describedby"].split())

    def test_a_value_for_a_field_the_form_lacks_is_refused(self):
        source = EDITED.replace("distance_miles=miles", "miles=miles")

        with self.assertRaisesMessage(TemplateSyntaxError, "miles"):
            draw("/example/runs/?edit=example-01", source=source)
