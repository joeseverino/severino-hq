"""Every form field renders through templates/django/forms/field.html, which
FORM_RENDERER finds before Django's own: what a field says about itself reaches
the control, and every description the control names is on the page."""

from django import forms
from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.tests.markup import Element, all_elements, select, with_class

User = get_user_model()


def group_of(page, name: str) -> Element:
    """The field group holding the control named ``name``."""

    (control,) = [element for element in select(page, "name", name) if element.tag in {"input", "select", "textarea"}]
    return next(element for element in reversed(control.ancestors) if "field" in element.classes)


def label_of(page, name: str) -> Element:
    (control,) = [element for element in select(page, "name", name) if element.tag in {"input", "select", "textarea"}]
    held = [element for element in control.ancestors if element.tag == "label"]
    return held[-1] if held else next(label for label in select(page, "for", control.attrs["id"]))


def unresolved_descriptions(page) -> list[str]:
    """Every id an aria-describedby names that no element on the page carries."""

    ids = {element.attrs["id"] for element in all_elements(page) if "id" in element.attrs}
    return [
        named
        for element in all_elements(page)
        for named in element.attrs.get("aria-describedby", "").split()
        if named not in ids
    ]


class CommandFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(username="fields", email="fields@example.test", password="x-1234-y")
        ManagedResource.objects.create(key="example-certificate", kind="tls.certificate", spec={})

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_confirmation_is_a_box_ticked_by_its_sentence_in_the_callout_the_form_marks(self):
        page = self.client.get("/commands/certificate.renew/")

        group = group_of(page, "__confirm_effect")
        self.assertIn("command-confirm", group.classes)
        self.assertIn("check-row", label_of(page, "__confirm_effect").classes)
        self.assertEqual(len(with_class(page, "command-confirm")), 1)

    def test_a_refused_submission_names_each_error_and_help_beside_its_control(self):
        page = self.client.post("/commands/certificate.renew/", {})

        for name in ("__target", "__confirm_effect"):
            with self.subTest(name=name):
                self.assertIn("has-errors", group_of(page, name).classes)
                (control,) = [element for element in select(page, "name", name) if element.tag != "option"]
                self.assertIn(f"{control.attrs['id']}_error", control.attrs["aria-describedby"].split())
        (target,) = select(page, "name", "__target")
        self.assertIn(f"{target.attrs['id']}_helptext", target.attrs["aria-describedby"].split())
        self.assertEqual(unresolved_descriptions(page), [])

    def test_a_required_field_says_so_in_its_label_and_an_optional_one_does_not(self):
        page = self.client.get("/commands/project.create/")

        self.assertTrue(label_of(page, "name").words.endswith("*"))
        self.assertFalse(label_of(page, "notes").words.endswith("*"))


class _ExampleForm(forms.Form):
    title = forms.CharField(help_text="What the example is called.")
    region = forms.CharField(required=False)
    enabled = forms.BooleanField(required=False, help_text="Off leaves it as it is.")
    token = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].wide = True
        self.fields["region"].change_effect = "Changing this moves the example."


class FormRenderingTests(SimpleTestCase):
    def render(self, source: str, form: forms.Form) -> str:
        return Template(source).render(Context({"form": form}))

    def test_a_whole_form_and_a_single_field_wear_the_same_markup(self):
        form = _ExampleForm()

        whole = self.render("{{ form }}", form)
        single = self.render("{{ form.title.as_field_group }}", form)

        self.assertEqual(group_of(single, "title").classes, group_of(whole, "title").classes)
        self.assertIn("is-wide", group_of(whole, "title").classes)

    def test_what_a_field_declares_reaches_its_group(self):
        page = self.render("{{ form }}", _ExampleForm())

        group = group_of(page, "region")
        self.assertEqual(group.attrs["data-change-effect"], "Changing this moves the example.")
        self.assertIn("check-row", label_of(page, "enabled").classes)
        self.assertEqual(unresolved_descriptions(page), [])

    def test_a_hidden_field_is_no_group_of_its_own(self):
        page = self.render("{{ form }}", _ExampleForm())

        (token,) = select(page, "name", "token")
        self.assertFalse([element for element in token.ancestors if "field" in element.classes])
