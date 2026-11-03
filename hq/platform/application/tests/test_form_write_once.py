"""A form mixin's write acts once per drawing of its form, as a capability's does.

Each mixin in ``writes``: a post writes, the same post again is answered as
the first was, the same key with a changed form is refused and the form drawn
again with a fresh key, and a refused write leaves its key free.
"""

from datetime import date
from typing import Any
from unittest import mock

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.template import RequestContext, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.views.generic import FormView

from hq.domains.calendars.models import Entry
from hq.domains.calendars.views import EntryCreateView, EntryDeleteView, EntryUpdateView
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project

from .. import records
from ..expenses import ExpenseCommand, save_expense
from ..web_writes import RETRY_KEY_FIELD, run_posted
from .markup import text
from .writers import drawn_key, form_keyed

# The delete page's one button posts the page's shared form.
CONFIRM = ("id", "hq-post")
ENTRY = {"title": "Haircut", "starts_on": "2026-10-17", "repeat": "", "interval": 1}
EXPENSE = {
    "date": "2026-07-25",
    "vendor": "Example Store",
    "item": "Cable",
    "category": "miscellaneous",
    "total_cost": "1.00",
    "business_use_percentage": "100",
}


def _entry(**fields: Any) -> Entry:
    entry = Entry(**{"title": "Entry", "starts_on": date(2026, 10, 1), **fields})
    entry.full_clean()
    entry.save()
    return entry


def _expense(**fields: Any) -> Expense:
    expense = Expense(**{"date": date(2026, 7, 25), "vendor": "Example Store", "item": "Cable", **fields})
    expense.full_clean()
    expense.save()
    return expense


def _once_refused(real, error: Exception):
    """``real``, refusing with ``error`` the first time it is called."""

    calls = []

    def refusing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise error
        return real(*args, **kwargs)

    return refusing


class FormWriteCase(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("operator", password="unused-test-pass"))

    def assertReplayed(self, first, again, *, rows, written):
        """The second answer is the first's, and what ``rows`` reads is as the first left it."""

        self.assertEqual(again.status_code, first.status_code)
        self.assertEqual(again["Location"], first["Location"])
        self.assertEqual(rows(), written)

    def assertRedrawnRefusing(self, response, posted, code: int = 409, form: tuple[str, str] = ("class", "form")):
        """The form again, refused with ``code``, carrying a key of its own."""

        self.assertEqual(response.status_code, code)
        self.assertNotEqual(drawn_key(response, *form), posted[RETRY_KEY_FIELD])


class ServiceCreateTests(FormWriteCase):
    def test_the_same_post_again_creates_one_event_and_goes_to_it(self):
        posted = form_keyed(ENTRY)

        first = self.client.post(reverse("calendar:entry_new"), posted)
        again = self.client.post(reverse("calendar:entry_new"), posted)

        (entry,) = Entry.objects.all()
        self.assertRedirects(first, entry.get_absolute_url(), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=Entry.objects.count, written=1)

    def test_the_same_key_with_another_form_is_refused_on_the_form(self):
        posted = form_keyed(ENTRY)
        self.client.post(reverse("calendar:entry_new"), posted)

        changed = self.client.post(reverse("calendar:entry_new"), {**posted, "title": "Dentist"})

        self.assertRedrawnRefusing(changed, posted)
        self.assertEqual(list(Entry.objects.values_list("title", flat=True)), ["Haircut"])

    def test_a_refused_create_frees_its_key(self):
        posted = form_keyed(ENTRY)
        refusing = _once_refused(EntryCreateView.service, ValueError("Refused by the service."))
        self.client.raise_request_exception = False

        with mock.patch.object(EntryCreateView, "service", staticmethod(refusing)):
            refused = self.client.post(reverse("calendar:entry_new"), posted)
            self.assertFalse(Entry.objects.exists())
            landed = self.client.post(reverse("calendar:entry_new"), posted)

        self.assertEqual(refused.status_code, 500)
        self.assertRedirects(landed, Entry.objects.get().get_absolute_url(), fetch_redirect_response=False)

    def test_reload_is_handed_the_services_result_and_its_own_call_replays_on_a_resend(self):
        """An extension's ``reload`` reads past the identity, and may run a
        capability of its own after the save under the key the form posted."""

        seen = []
        real = EntryCreateView.reload

        def reload(view, result):
            ran = run_posted(view.request, "project.create", {"name": "Example", "slug": "example-after-the-save"})
            seen.append((dict(result), ran.replayed))
            return real(view, result)

        posted = form_keyed(ENTRY)
        with mock.patch.object(EntryCreateView, "reload", reload):
            self.client.post(reverse("calendar:entry_new"), posted)
            self.client.post(reverse("calendar:entry_new"), posted)

        (first, _), (again, _) = seen
        self.assertEqual(again, first)
        self.assertIn("entry", first)
        self.assertEqual([replayed for _, replayed in seen], [False, True])
        self.assertEqual(Project.objects.count(), 1)

    def test_a_post_without_a_key_is_refused_on_the_form(self):
        refused = self.client.post(reverse("calendar:entry_new"), ENTRY)

        self.assertRedrawnRefusing(refused, form_keyed(), code=400)
        self.assertFalse(Entry.objects.exists())


class ServiceUpdateTests(FormWriteCase):
    def test_the_same_post_again_amends_once_and_goes_to_the_event(self):
        entry = _entry()
        posted = form_keyed(ENTRY)

        first = self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), posted)
        entry.refresh_from_db()
        again = self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), posted)

        self.assertRedirects(first, entry.get_absolute_url(), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=lambda: Entry.objects.get().updated_at, written=entry.updated_at)

    def test_the_same_key_with_another_form_is_refused_on_the_form(self):
        entry = _entry()
        posted = form_keyed(ENTRY)
        self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), posted)

        changed = self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), {**posted, "title": "Dentist"})

        self.assertRedrawnRefusing(changed, posted)
        entry.refresh_from_db()
        self.assertEqual(entry.title, "Haircut")

    def test_a_refused_amendment_frees_its_key(self):
        entry = _entry()
        posted = form_keyed(ENTRY)
        refusing = _once_refused(EntryUpdateView.service, ValueError("Refused by the service."))
        self.client.raise_request_exception = False

        with mock.patch.object(EntryUpdateView, "service", staticmethod(refusing)):
            self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), posted)
            entry.refresh_from_db()
            self.assertEqual(entry.title, "Entry")
            self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), posted)

        entry.refresh_from_db()
        self.assertEqual(entry.title, "Haircut")


class ServiceDeleteTests(FormWriteCase):
    def test_the_same_post_again_deletes_once_and_goes_back_to_the_list(self):
        entry = _entry()
        posted = form_keyed()

        first = self.client.post(reverse("calendar:entry_delete", args=[entry.uid]), posted)
        again = self.client.post(reverse("calendar:entry_delete", args=[entry.uid]), posted)

        self.assertRedirects(first, reverse("calendar:month"), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=Entry.objects.count, written=0)

    def test_the_same_key_sent_to_another_event_is_refused_on_the_form(self):
        kept, other = _entry(title="Kept"), _entry(title="Other")
        posted = form_keyed()
        self.client.post(reverse("calendar:entry_delete", args=[other.uid]), posted)

        refused = self.client.post(reverse("calendar:entry_delete", args=[kept.uid]), posted)

        self.assertRedrawnRefusing(refused, posted, form=CONFIRM)
        self.assertIn("different request", text(refused))
        self.assertEqual(list(Entry.objects.values_list("title", flat=True)), ["Kept"])

    def test_a_refused_delete_frees_its_key(self):
        entry = _entry()
        posted = form_keyed()
        refusing = _once_refused(EntryDeleteView.service, ValueError("Refused by the service."))
        self.client.raise_request_exception = False

        with mock.patch.object(EntryDeleteView, "service", staticmethod(refusing)):
            self.client.post(reverse("calendar:entry_delete", args=[entry.uid]), posted)
            self.assertTrue(Entry.objects.exists())
            self.client.post(reverse("calendar:entry_delete", args=[entry.uid]), posted)

        self.assertFalse(Entry.objects.exists())


class RecordFormTests(FormWriteCase):
    def test_the_same_post_again_creates_one_record_and_goes_to_it(self):
        posted = form_keyed(EXPENSE)

        first = self.client.post(reverse("expenses:create"), posted)
        again = self.client.post(reverse("expenses:create"), posted)

        (expense,) = Expense.objects.all()
        self.assertRedirects(first, expense.get_absolute_url(), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=Expense.objects.count, written=1)

    def test_the_same_post_again_amends_once(self):
        expense = _expense()
        posted = form_keyed({**EXPENSE, "item": "Charger"})

        first = self.client.post(reverse("expenses:edit", args=[expense.pk]), posted)
        expense.refresh_from_db()
        again = self.client.post(reverse("expenses:edit", args=[expense.pk]), posted)

        self.assertRedirects(first, expense.get_absolute_url(), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=lambda: Expense.objects.get().updated_at, written=expense.updated_at)

    def test_the_same_key_with_another_form_is_refused_on_the_form(self):
        posted = form_keyed(EXPENSE)
        self.client.post(reverse("expenses:create"), posted)

        changed = self.client.post(reverse("expenses:create"), {**posted, "item": "Charger"})

        self.assertRedrawnRefusing(changed, posted)
        self.assertEqual(list(Expense.objects.values_list("item", flat=True)), ["Cable"])

    def test_a_refused_save_frees_its_key(self):
        posted = form_keyed(EXPENSE)
        refusing = _once_refused(records.save_form, ValidationError({"vendor": "Refused by the model."}))

        with mock.patch("hq.platform.application.writes.save_form", refusing):
            refused = self.client.post(reverse("expenses:create"), posted)
            landed = self.client.post(reverse("expenses:create"), posted)

        self.assertIn("Refused by the model.", refused.context["form"].errors["vendor"])
        self.assertRedirects(landed, Expense.objects.get().get_absolute_url(), fetch_redirect_response=False)


class RecordDeleteTests(FormWriteCase):
    def test_the_same_post_again_deletes_once_and_goes_back_to_the_list(self):
        expense = _expense()
        posted = form_keyed()

        first = self.client.post(reverse("expenses:delete", args=[expense.pk]), posted)
        again = self.client.post(reverse("expenses:delete", args=[expense.pk]), posted)

        self.assertRedirects(first, reverse("expenses:list"), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=Expense.objects.count, written=0)

    def test_the_same_key_sent_to_another_record_is_refused_on_the_form(self):
        kept, other = _expense(item="Kept"), _expense(item="Other")
        posted = form_keyed()
        self.client.post(reverse("expenses:delete", args=[other.pk]), posted)

        refused = self.client.post(reverse("expenses:delete", args=[kept.pk]), posted)

        self.assertRedrawnRefusing(refused, posted, form=CONFIRM)
        self.assertIn("different request", text(refused))
        self.assertEqual(list(Expense.objects.values_list("item", flat=True)), ["Kept"])

    def test_a_refused_delete_frees_its_key(self):
        expense = _expense()
        posted = form_keyed()
        refusing = _once_refused(records.delete_instance, ValueError("Refused by the deleter."))
        self.client.raise_request_exception = False

        with mock.patch("hq.platform.application.writes.delete_instance", refusing):
            self.client.post(reverse("expenses:delete", args=[expense.pk]), posted)
            self.assertTrue(Expense.objects.exists())
            self.client.post(reverse("expenses:delete", args=[expense.pk]), posted)

        self.assertFalse(Expense.objects.exists())


class ExpenseCommandForm(forms.Form):
    date = forms.DateField()
    vendor = forms.CharField()
    item = forms.CharField()


def _command_view():
    """A page whose submit is ``save_expense``, as an extension's command form is."""

    from ..writes import CommandFormMixin

    class ExpenseCommandView(CommandFormMixin, FormView):
        form_class = ExpenseCommandForm
        service = staticmethod(save_expense)
        command = ExpenseCommand
        success_url_name = "expenses:list"
        success_message = "Expense saved."

        def render_to_response(self, context, **response_kwargs):
            page = Template(
                "{% load write_tags %}<form method='post' class='form'>{% write_token %}{{ form.non_field_errors }}{{ form }}</form>"
            )
            return HttpResponse(page.render(RequestContext(self.request, context)), **response_kwargs)

    return ExpenseCommandView


class CommandFormTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user("operator", password="unused-test-pass")
        self.view = _command_view().as_view()

    def post(self, data):
        request = RequestFactory().post("/example/expense/", data)
        request.user = self.person
        SessionMiddleware(lambda request: HttpResponse()).process_request(request)
        request._messages = FallbackStorage(request)
        return self.view(request), request

    def test_the_same_post_again_saves_once_and_says_so_again(self):
        posted = form_keyed({"date": "2026-07-25", "vendor": "Example Store", "item": "Cable"})

        first, _ = self.post(posted)
        again, request = self.post(posted)

        self.assertRedirects(first, reverse("expenses:list"), fetch_redirect_response=False)
        self.assertEqual(again["Location"], first["Location"])
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual([str(message) for message in get_messages(request)], ["Expense saved."])

    def test_the_same_key_with_another_form_is_refused_on_the_form(self):
        posted = form_keyed({"date": "2026-07-25", "vendor": "Example Store", "item": "Cable"})
        self.post(posted)

        changed, _ = self.post({**posted, "item": "Charger"})

        self.assertEqual(changed.status_code, 409)
        self.assertNotEqual(drawn_key(changed, "class", "form"), posted[RETRY_KEY_FIELD])
        self.assertEqual(list(Expense.objects.values_list("item", flat=True)), ["Cable"])

    def test_the_same_key_and_body_sent_to_another_form_is_refused_there(self):
        class AnotherExpenseView(self.view.view_class):
            success_message = "Another form's answer."

        posted = form_keyed({"date": "2026-07-25", "vendor": "Example Store", "item": "Cable"})
        self.post(posted)
        self.view = AnotherExpenseView.as_view()

        refused, _ = self.post(posted)

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(Expense.objects.count(), 1)

    def test_a_refused_command_frees_its_key(self):
        posted = form_keyed({"date": "2026-07-25", "vendor": "Example Store", "item": "Cable"})
        refusing = _once_refused(save_expense, ValueError("Refused by the domain."))

        with mock.patch.object(self.view.view_class, "service", staticmethod(refusing)):
            refused, _ = self.post(posted)
            landed, _ = self.post(posted)

        self.assertContains(refused, "Refused by the domain.")
        self.assertRedirects(landed, reverse("expenses:list"), fetch_redirect_response=False)
        self.assertEqual(Expense.objects.count(), 1)
