"""A record's form writes through its capability, once per drawing of the form.

Each form mixin in ``writes`` keeps five promises, held here once over a site
of every mixin: the same post again acts once and is answered as the first
was, the same key on another request is refused and the form drawn again with
a fresh key, a refused write leaves its key free, a person without the grant
is refused on the form, and a post without a key is refused.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any, ClassVar
from unittest import mock

from django import forms
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import transaction
from django.http import HttpResponse
from django.template import RequestContext, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.views.generic import FormView

from hq.domains.calendars.models import Entry
from hq.domains.calendars.views import EntryCreateView
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.markup_testing import with_class
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator

from .. import capabilities, writes
from ..capabilities import Ran, command_spec
from ..plugin_testing import form_keyed
from ..security import OPERATOR_CAPABILITIES, Principal
from ..web_writes import RETRY_KEY_FIELD, run_posted
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
    """The capability's run, refusing the first call from under the key it reserved."""

    calls = []
    real = capabilities._run

    def refusing(*args, **kwargs):
        calls.append(1)
        return REFUSED if len(calls) == 1 else real(*args, **kwargs)

    return mock.patch.object(capabilities, "_run", refusing)


def as_principal(principal: Principal):
    return mock.patch("hq.platform.application.web_writes.web_principal", return_value=principal)


def without_the_grant(capability: str) -> Principal:
    """An operator holding everything but what ``capability`` requires."""

    return Principal("operator", "web", OPERATOR_CAPABILITIES - set(command_spec(capability).required_capabilities))


class SignedIn(TestCase):
    def setUp(self):
        self.person = operator(self.client)


class Site:
    """One form's write as a person posts it from its page, on rows of its own."""

    # The capability its post runs.
    runs: ClassVar[str]
    # A valid form, without its key.
    data: ClassVar[dict[str, Any]] = {}
    # The form on its page that carries the key.
    form: ClassVar[tuple[str, str]] = ("class", "form")

    def __init__(self, case: SignedIn) -> None:
        self.case = case

    @property
    def url(self) -> str:
        raise NotImplementedError

    def post(self, data: dict[str, Any]) -> HttpResponse:
        return self.case.client.post(self.url, data)

    def other(self, posted: dict[str, Any]) -> HttpResponse:
        """``posted``'s key sent with another request."""

        raise NotImplementedError

    def rows(self) -> list[dict[str, Any]]:
        """What its write changes, as stored now."""

        raise NotImplementedError

    def went(self) -> str:
        """Where its write goes once it lands."""

        raise NotImplementedError

    def said(self, response: HttpResponse) -> str:
        """The refusal ``response`` says where the person reads it: above the form."""

        return " ".join(response.context["form"].non_field_errors())

    def replayed(self, again: HttpResponse) -> None:
        """What a site's resend says beyond where it goes."""


class EntryCreate(Site):
    runs = "calendar.entry.create"
    data = ENTRY

    @property
    def url(self) -> str:
        return reverse("calendar:entry_new")

    def other(self, posted):
        return self.post({**posted, "title": "Dentist"})

    def rows(self):
        return list(Entry.objects.values())

    def went(self):
        return Entry.objects.get().get_absolute_url()

    def replayed(self, again):
        created = AuditLog.objects.get(action=AuditLog.Action.CREATED, object_id=str(Entry.objects.get().pk))
        self.case.assertEqual(created.metadata["operation"], self.runs)


class EntryEdit(EntryCreate):
    runs = "calendar.entry.update"

    def __init__(self, case):
        super().__init__(case)
        self.entry = _entry()

    @property
    def url(self):
        return reverse("calendar:entry_edit", args=[self.entry.uid])

    def went(self):
        # The event's address names its day, which the edit moved.
        return Entry.objects.get(pk=self.entry.pk).get_absolute_url()

    def replayed(self, again):
        pass


class _Deleting(Site):
    form = CONFIRM

    def said(self, response):
        # The confirmation page draws no form of its own, so a refusal is a message.
        return " ".join(str(message) for message in get_messages(response.wsgi_request))


class EntryDelete(_Deleting):
    runs = "calendar.entry.delete"

    def __init__(self, case):
        super().__init__(case)
        self.entry, self.another = _entry(title="Kept"), _entry(title="Other")

    @property
    def url(self):
        return reverse("calendar:entry_delete", args=[self.entry.uid])

    def other(self, posted):
        return self.case.client.post(reverse("calendar:entry_delete", args=[self.another.uid]), posted)

    def rows(self):
        return list(Entry.objects.values())

    def went(self):
        return reverse("calendar:month")


class ExpenseCreate(Site):
    runs = "expense.create"
    data = EXPENSE

    @property
    def url(self):
        return reverse("expenses:create")

    def other(self, posted):
        return self.post({**posted, "item": "Charger"})

    def rows(self):
        return list(Expense.objects.values())

    def went(self):
        return Expense.objects.get().get_absolute_url()


class ExpenseEdit(ExpenseCreate):
    runs = "expense.update"
    data = {**EXPENSE, "item": "Charger"}

    def __init__(self, case):
        super().__init__(case)
        self.expense = _expense()

    @property
    def url(self):
        return reverse("expenses:edit", args=[self.expense.pk])

    def other(self, posted):
        return self.post({**posted, "item": "Adapter"})


class ProjectCreate(Site):
    """A record whose resend the first post made invalid: its slug is now taken."""

    runs = "project.create"
    data = {"name": "Example", "slug": "example", "category": "other", "status": "idea"}

    @property
    def url(self):
        return reverse("projects:create")

    def other(self, posted):
        return self.post({**posted, "name": "Other", "slug": "other"})

    def rows(self):
        return list(Project.objects.values())

    def went(self):
        return Project.objects.get().get_absolute_url()


class ExpenseDelete(_Deleting):
    runs = "expense.delete"

    def __init__(self, case):
        super().__init__(case)
        self.expense, self.another = _expense(item="Kept"), _expense(item="Other")

    @property
    def url(self):
        return reverse("expenses:delete", args=[self.expense.pk])

    def other(self, posted):
        return self.case.client.post(reverse("expenses:delete", args=[self.another.pk]), posted)

    def rows(self):
        return list(Expense.objects.values())

    def went(self):
        return reverse("expenses:list")


class ExpenseCommandForm(forms.Form):
    date = forms.DateField()
    vendor = forms.CharField()
    item = forms.CharField()


def _command_view():
    """A page whose submit is a command, as an extension's command form is."""

    class ExpenseCommandView(writes.CapabilityCommandMixin, FormView):
        form_class = ExpenseCommandForm
        submits = "expense.create"
        success_url_name = "expenses:list"

        def render_to_response(self, context, **response_kwargs):
            page = Template(
                "{% load write_tags %}<form method='post' class='form'>{% write_token %}{{ form.non_field_errors }}{{ form }}</form>"
            )
            return HttpResponse(page.render(RequestContext(self.request, context)), **response_kwargs)

    return ExpenseCommandView


class Command(Site):
    """The published ``CapabilityCommandMixin``: its command through the runner."""

    runs = "expense.create"
    data = {"date": "2026-07-25", "vendor": "Example Store", "item": "Cable"}

    def __init__(self, case, view: type | None = None):
        super().__init__(case)
        self.view = view or _command_view()

    def post(self, data):
        request = RequestFactory().post("/example/expense/", data)
        request.user = self.case.person
        SessionMiddleware(lambda request: HttpResponse()).process_request(request)
        request._messages = FallbackStorage(request)
        self.request = request
        return self.view.as_view()(request)

    def said(self, response):
        # No route reaches the page, so the refusal is read from what it drew.
        return " ".join(each.words for each in with_class(response, "nonfield"))

    def other(self, posted):
        return self.post({**posted, "item": "Charger"})

    def rows(self):
        return list(Expense.objects.values())

    def went(self):
        return reverse("expenses:list")

    def replayed(self, again):
        said = [str(message) for message in get_messages(self.request)]
        self.case.assertEqual(len(said), 1)
        self.case.assertIn(str(Expense.objects.get()), said[0])


# A site of every form mixin: CapabilityFormMixin, CapabilityDeleteMixin,
# RecordFormMixin, RecordDeleteMixin and CapabilityCommandMixin.
SITES: tuple[type[Site], ...] = (
    EntryCreate,
    EntryEdit,
    EntryDelete,
    ExpenseCreate,
    ExpenseEdit,
    ProjectCreate,
    ExpenseDelete,
    Command,
)


class FormWriteOnceTests(SignedIn):
    @contextmanager
    def at(self, kind: type[Site]) -> Iterator[Site]:
        """``kind`` as a subtest, signed in afresh so no message carries over,
        and what it wrote rolled back on leaving."""

        with self.subTest(site=kind.__name__), transaction.atomic():
            self.client = self.client_class()
            self.client.force_login(self.person)
            yield kind(self)
            transaction.set_rollback(True)

    def assertRedrawnRefusing(self, site: Site, response, code: int, posted: dict[str, Any] | None = None):
        """The form again, refused with ``code``, carrying a key of its own."""

        self.assertEqual(response.status_code, code)
        key = drawn_key(response, *site.form)
        self.assertTrue(key)
        if posted is not None:
            self.assertNotEqual(key, posted[RETRY_KEY_FIELD])

    def test_the_same_post_again_acts_once_and_is_answered_as_the_first(self):
        for kind in SITES:
            with self.at(kind) as site:
                posted = form_keyed(site.data)
                before = site.rows()

                first = site.post(posted)
                written = site.rows()
                again = site.post(posted)

                self.assertNotEqual(written, before)
                self.assertRedirects(first, site.went(), fetch_redirect_response=False)
                self.assertEqual((again.status_code, again["Location"]), (first.status_code, first["Location"]))
                self.assertEqual(site.rows(), written)
                site.replayed(again)

    def test_the_same_key_on_another_request_is_refused_on_the_form(self):
        for kind in SITES:
            with self.at(kind) as site:
                posted = form_keyed(site.data)
                site.post(posted)
                written = site.rows()

                changed = site.other(posted)

                self.assertRedrawnRefusing(site, changed, 409, posted)
                self.assertIn("different request", site.said(changed))
                self.assertEqual(site.rows(), written)

    def test_a_refused_write_is_said_on_the_form_and_frees_its_key(self):
        for kind in SITES:
            with self.at(kind) as site:
                posted = form_keyed(site.data)
                before = site.rows()

                with refused_once():
                    refused = site.post(posted)
                    self.assertEqual(site.rows(), before)
                    landed = site.post(posted)

                self.assertIn("Refused by the capability.", site.said(refused))
                self.assertRedirects(landed, site.went(), fetch_redirect_response=False)
                self.assertNotEqual(site.rows(), before)

    def test_a_person_without_the_grant_is_refused_on_the_form(self):
        for kind in SITES:
            with self.at(kind) as site:
                before = site.rows()

                with as_principal(without_the_grant(site.runs)):
                    refused = site.post(form_keyed(site.data))

                self.assertEqual(refused.status_code, 403)
                self.assertTrue(site.said(refused))
                self.assertEqual(site.rows(), before)

    def test_a_post_without_a_key_is_refused_on_the_form(self):
        for kind in SITES:
            with self.at(kind) as site:
                before = site.rows()

                refused = site.post(dict(site.data))

                self.assertRedrawnRefusing(site, refused, 400)
                self.assertEqual(site.rows(), before)


class FormWriteTests(SignedIn):
    """What one mixin does beyond the five promises."""

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

    def test_a_refusal_naming_a_field_lands_beside_it(self):
        posted = form_keyed(EXPENSE)
        beside = {
            "ok": False,
            "error": {"code": "invalid_input", "message": "Refused.", "details": [{"loc": ["vendor"], "msg": "No."}]},
        }

        with mock.patch.object(writes, "run_posted", return_value=Ran(beside)):
            refused = self.client.post(reverse("expenses:create"), posted)

        self.assertEqual(refused.context["form"].errors["vendor"], ["No."])
        self.assertFalse(refused.context["form"].non_field_errors())

    def test_the_record_a_command_runs_on_is_sent_as_its_target(self):
        class OnOne(_command_view()):
            def target(self):
                return 7

        site = Command(self, OnOne)

        with mock.patch.object(writes, "run_posted", return_value=Ran({"ok": True})) as ran:
            landed = site.post(form_keyed(site.data))

        self.assertRedirects(landed, reverse("expenses:list"), fetch_redirect_response=False)
        self.assertEqual((ran.call_args.args[1], ran.call_args.kwargs["target"]), ("expense.create", 7))
