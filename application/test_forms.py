from django.test import SimpleTestCase

from assets.forms import AssetForm
from expenses.forms import ExpenseForm

from .forms import BusinessUseMixin


class BusinessUseTests(SimpleTestCase):
    FORMS = (AssetForm, ExpenseForm)

    def cleaned(self, form_class, value):
        form = form_class(data={})
        form.cleaned_data = {"business_use_percentage": value}
        return form.clean_business_use_percentage()

    def test_both_forms_hold_the_one_rule(self):
        for form_class in self.FORMS:
            with self.subTest(form=form_class.__name__):
                self.assertTrue(issubclass(form_class, BusinessUseMixin))
                self.assertNotIn("clean_business_use_percentage", vars(form_class))

    def test_a_percentage_in_range_is_kept(self):
        for form_class in self.FORMS:
            with self.subTest(form=form_class.__name__):
                self.assertEqual(self.cleaned(form_class, "40"), 40)
                self.assertEqual(self.cleaned(form_class, None), 0)

    def test_a_percentage_out_of_range_is_refused(self):
        from django.forms import ValidationError

        for form_class in self.FORMS:
            for value in (150, -1):
                with self.subTest(form=form_class.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        self.cleaned(form_class, value)
