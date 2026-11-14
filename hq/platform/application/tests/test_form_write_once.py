"""A record's form writes through its capability, once per drawing of the form.

Each form mixin in ``writes``: a post writes, the same post again is answered
as the first was, the same key with a changed form is refused and the form
drawn again with a fresh key, a refused write leaves its key free, and a
person without the grant is refused on the form.
"""

from datetime import date
from typing import Any
from unittest import mock

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.template import RequestContext, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.views.generic import FormView

from hq.domains.calendars.models import Entry
from hq.domains.calendars.views import EntryCreateView
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.core.models import AuditLog

from .. import writes
from ..expenses import ExpenseCommand, save_expense
from ..plugin_testing import form_keyed
from ..security import OPERATOR_CAPABILITIES, Principal
from ..web_writes import RETRY_KEY_FIELD, run_posted
from .markup import text
from .writers import drawn_key

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
REFUSED = {"ok": False, "error": {"code": "conflict", "message": "Refused by the capability."}}


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


def refused_once():
    """``run_posted`` as the form mixins call it, refusing the first call."""

    calls = []

    def refusing(request, *args, **kwargs):
        calls.append(1)
        return writes.Ran(REFUSED) if len(calls) == 1 else run_posted(request, *args, **kwargs)

    return mock.patch.object(writes, "run_posted", refusing)


def as_principal(principal: Principal):
    return mock.patch("hq.platform.application.web_writes.web_principal", return_value=principal)


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


class CapabilityFormTests(FormWriteCase):
    def test_the_same_post_again_creates_one_event_and_goes_to_it(self):
        posted = form_keyed(ENTRY)

        first = self.client.post(reverse("calendar:entry_new"), posted)
        again = self.client.post(reverse("calendar:entry_new"), posted)

        (entry,) = Entry.objects.all()
        self.assertRedirects(first, entry.get_absolute_url(), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=Entry.objects.count, written=1)
        created = AuditLog.objects.get(action=AuditLog.Action.CREATED, object_id=str(entry.pk))
        self.assertEqual(created.metadata["operation"], "calendar.entry.create")

    def test_the_same_key_with_another_form_is_refused_on_the_form(self):
        posted = form_keyed(ENTRY)
        self.client.post(reverse("calendar:entry_new"), posted)

        changed = self.client.post(reverse("calendar:entry_new"), {**posted, "title": "Dentist"})

        self.assertRedrawnRefusing(changed, posted)
        self.assertEqual(list(Entry.objects.values_list("title", flat=True)), ["Haircut"])

    def test_a_refused_create_is_said_on_the_form_and_frees_its_key(self):
        posted = form_keyed(ENTRY)

        with refused_once():
            refused = self.client.post(reverse("calendar:entry_new"), posted)
            self.assertFalse(Entry.objects.exists())
            landed = self.client.post(reverse("calendar:entry_new"), posted)

        self.assertIn("Refused by the capability.", refused.context["form"].non_field_errors())
        self.assertRedirects(landed, Entry.objects.get().get_absolute_url(), fetch_redirect_response=False)

    def test_reload_is_handed_the_answer_and_its_own_call_replays_on_a_resend(self):
        """A view's ``reload`` reads past the identity, and may run a capability
        of its own after the save under the key the form posted. One that adds
        a record the form's pickers offer would change what the resend sends,
        which the key then refuses as a different request."""

        seen = []
        real = EntryCreateView.reload

        def reload(view, answer):
            ran = run_posted(view.request, "calendar.entry.create", {"title": "After", "starts_on": "2026-10-18"})
            seen.append((dict(answer), ran.replayed))
            return real(view, answer)

        posted = form_keyed(ENTRY)
        with mock.patch.object(EntryCreateView, "reload", reload):
            self.client.post(reverse("calendar:entry_new"), posted)
            self.client.post(reverse("calendar:entry_new"), posted)

        (first, _), (again, _) = seen
        self.assertEqual(again, first)
        self.assertIn("entry", first)
        self.assertEqual([replayed for _, replayed in seen], [False, True])
        self.assertEqual(Entry.objects.count(), 2)

    def test_a_post_without_a_key_is_refused_on_the_form(self):
        refused = self.client.post(reverse("calendar:entry_new"), ENTRY)

        self.assertRedrawnRefusing(refused, form_keyed(), code=400)
        self.assertFalse(Entry.objects.exists())

    def test_the_same_post_again_amends_once_and_goes_to_the_event(self):
        entry = _entry()
        posted = form_keyed(ENTRY)

        first = self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), posted)
        entry.refresh_from_db()
        again = self.client.post(reverse("calendar:entry_edit", args=[entry.uid]), posted)

        self.assertRedirects(first, entry.get_absolute_url(), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=lambda: Entry.objects.get().updated_at, written=entry.updated_at)

    def test_a_person_without_the_grant_is_refused_on_the_form(self):
        posted = form_keyed(ENTRY)

        with as_principal(Principal("operator", "web", OPERATOR_CAPABILITIES - {"write_calendar"})):
            refused = self.client.post(reverse("calendar:entry_new"), posted)

        self.assertEqual(refused.status_code, 403)
        self.assertTrue(refused.context["form"].non_field_errors())
        self.assertFalse(Entry.objects.exists())


class CapabilityDeleteTests(FormWriteCase):
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

        with refused_once():
            refused = self.client.post(reverse("calendar:entry_delete", args=[entry.uid]), posted)
            self.assertTrue(Entry.objects.exists())
            self.client.post(reverse("calendar:entry_delete", args=[entry.uid]), posted)

        self.assertIn("Refused by the capability.", text(refused))
        self.assertFalse(Entry.objects.exists())


class RecordFormTests(FormWriteCase):
    def test_the_same_post_again_creates_one_record_and_goes_to_it(self):
        posted = form_keyed(EXPENSE)

        first = self.client.post(reverse("expenses:create"), posted)
        again = self.client.post(reverse("expenses:create"), posted)

        (expense,) = Expense.objects.all()
        self.assertRedirects(first, expense.get_absolute_url(), fetch_redirect_response=False)
        self.assertReplayed(first, again, rows=Expense.objects.count, written=1)

    def test_a_resend_its_first_post_made_invalid_is_answered_as_the_first(self):
        posted = form_keyed({"name": "Example", "slug": "example", "category": "other", "status": "idea"})

        first = self.client.post(reverse("projects:create"), posted)
        again = self.client.post(reverse("projects:create"), posted)

        self.assertReplayed(first, again, rows=Project.objects.count, written=1)

    def test_the_same_post_again_amends_once(self):
        expense = _expense()
        posted = form_keyed({**EXPENSE, "item": "Charger"})

        first = self.client.post(reverse("expenses:edit", args=[expense.pk]), posted)
        expense.refresh_from_db()
        again = self.client.post(reverse("expenses:edit", args=[expense.pk]), posted)

        self.assertRedirects(first, expense.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(expense.item, "Charger")
        self.assertReplayed(first, again, rows=lambda: Expense.objects.get().updated_at, written=expense.updated_at)

    def test_the_same_key_with_another_form_is_refused_on_the_form(self):
        posted = form_keyed(EXPENSE)
        self.client.post(reverse("expenses:create"), posted)

        changed = self.client.post(reverse("expenses:create"), {**posted, "item": "Charger"})

        self.assertRedrawnRefusing(changed, posted)
        self.assertEqual(list(Expense.objects.values_list("item", flat=True)), ["Cable"])

    def test_a_refusal_naming_a_field_lands_beside_it_and_frees_the_key(self):
        posted = form_keyed(EXPENSE)
        beside = {
            "ok": False,
            "error": {"code": "invalid_input", "message": "Refused.", "details": [{"loc": ["vendor"], "msg": "No."}]},
        }

        with mock.patch.object(writes, "run_posted", return_value=writes.Ran(beside)):
            refused = self.client.post(reverse("expenses:create"), posted)
        landed = self.client.post(reverse("expenses:create"), posted)

        self.assertEqual(refused.context["form"].errors["vendor"], ["No."])
        self.assertFalse(refused.context["form"].non_field_errors())
        self.assertRedirects(landed, Expense.objects.get().get_absolute_url(), fetch_redirect_response=False)

    def test_a_person_without_the_grant_is_refused_on_the_form(self):
        with as_principal(Principal("operator", "web", OPERATOR_CAPABILITIES - {"write_expenses"})):
            refused = self.client.post(reverse("expenses:create"), form_keyed(EXPENSE))

        self.assertEqual(refused.status_code, 403)
        self.assertTrue(refused.context["form"].non_field_errors())
        self.assertFalse(Expense.objects.exists())


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

    def test_a_person_without_the_grant_is_refused(self):
        expense = _expense()

        with as_principal(Principal("operator", "web", OPERATOR_CAPABILITIES - {"delete_expenses"})):
            refused = self.client.post(reverse("expenses:delete", args=[expense.pk]), form_keyed())

        self.assertEqual(refused.status_code, 403)
        self.assertTrue(Expense.objects.exists())


class ExpenseCommandForm(forms.Form):
    date = forms.DateField()
    vendor = forms.CharField()
    item = forms.CharField()


def _command_view(service=save_expense):
    """A page whose submit is ``service``, as an extension's command form is."""

    class ExpenseCommandView(writes.CommandFormMixin, FormView):
        form_class = ExpenseCommandForm
        command = ExpenseCommand
        success_url_name = "expenses:list"
        success_message = "Expense saved."

        def render_to_response(self, context, **response_kwargs):
            page = Template(
                "{% load write_tags %}<form method='post' class='form'>{% write_token %}{{ form.non_field_errors }}{{ form }}</form>"
            )
            return HttpResponse(page.render(RequestContext(self.request, context)), **response_kwargs)

    ExpenseCommandView.service = staticmethod(service)
    return ExpenseCommandView


class CommandFormTests(TestCase):
    """The published ``CommandFormMixin``: its service's capability through the
    runner, or a service that is none outside it."""

    POSTED = {"date": "2026-07-25", "vendor": "Example Store", "item": "Cable"}

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
        posted = form_keyed(self.POSTED)

        first, _ = self.post(posted)
        again, request = self.post(posted)

        self.assertRedirects(first, reverse("expenses:list"), fetch_redirect_response=False)
        self.assertEqual(again["Location"], first["Location"])
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual([str(message) for message in get_messages(request)], ["Expense saved."])

    def test_a_registered_service_is_run_as_its_capability(self):
        with as_principal(Principal("operator", "web", OPERATOR_CAPABILITIES - {"write_expenses"})):
            refused, _ = self.post(form_keyed(self.POSTED))

        self.assertEqual(refused.status_code, 403)
        self.assertFalse(Expense.objects.exists())

    def test_the_same_key_with_another_form_is_refused_on_the_form(self):
        posted = form_keyed(self.POSTED)
        self.post(posted)

        changed, _ = self.post({**posted, "item": "Charger"})

        self.assertEqual(changed.status_code, 409)
        self.assertNotEqual(drawn_key(changed, "class", "form"), posted[RETRY_KEY_FIELD])
        self.assertEqual(list(Expense.objects.values_list("item", flat=True)), ["Cable"])

    def test_a_service_that_is_no_capability_writes_once_outside_the_runner(self):
        calls = []

        def unregistered(command, *, principal):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("Refused by the domain.")
            return save_expense(command, principal=principal)

        self.view = _command_view(unregistered).as_view()
        posted = form_keyed(self.POSTED)

        with mock.patch.object(writes, "outside_the_runner", wraps=writes.outside_the_runner) as outside:
            refused, _ = self.post(posted)
            landed, _ = self.post(posted)
            again, _ = self.post(posted)

        self.assertContains(refused, "Refused by the domain.")
        self.assertRedirects(landed, reverse("expenses:list"), fetch_redirect_response=False)
        self.assertEqual(again["Location"], landed["Location"])
        self.assertEqual((Expense.objects.count(), len(calls)), (1, 2))
        self.assertTrue(outside.called)
