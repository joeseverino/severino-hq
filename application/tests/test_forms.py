from django.test import TestCase

from assets.forms import AssetForm
from expenses.forms import ExpenseForm

from ..business_use import MESSAGE


class BusinessUseTests(TestCase):
    """The forms show the model's rule; they do not hold a copy of it."""

    FORMS = (AssetForm, ExpenseForm)

    def test_neither_form_restates_the_rule(self):
        for form_class in self.FORMS:
            with self.subTest(form=form_class.__name__):
                self.assertNotIn("clean_business_use_percentage", vars(form_class))

    def test_a_percentage_out_of_range_is_refused_on_the_field(self):
        for form_class in self.FORMS:
            # -1 never reaches the model: the positive field's own form bound
            # refuses it first.
            for value, message in ((150, MESSAGE), (-1, "Ensure this value is greater than or equal to 0.")):
                with self.subTest(form=form_class.__name__, value=value):
                    form = form_class(data={"business_use_percentage": str(value)})
                    form.is_valid()
                    self.assertEqual(form.errors["business_use_percentage"], [message])
