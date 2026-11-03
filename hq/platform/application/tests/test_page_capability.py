"""A page opens only for a person whose role holds the capability it reads under."""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.views.generic import View

from hq.platform.application import roles
from hq.platform.application.security import Capability
from hq.platform.core.audit import record_event
from hq.platform.core.middleware import LoginRequiredMiddleware
from hq.platform.core.models import AuditLog
from hq.platform.core.test_runner import no_default_role
from hq_sdk.testing import holding
from hq_sdk.web import CapabilityRequiredMixin, capability_required

# The audit row a held request's stable link leads to.
HELD = uuid4()

# Written into the log by the test, so a refused page that read the log would show it.
SECRET = "example-only-the-audit-log-holds-this"


def _person(username: str, role: str):
    with no_default_role():
        user = get_user_model().objects.create_user(username)
    if role:
        roles.assign(user, role)
    return user


class ViewerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event = record_event(action=AuditLog.Action.UPDATED, type_label="Example", message=SECRET, required=True)
        AuditLog.objects.create(action=AuditLog.Action.CREATED, object_type="Approval", object_id=str(HELD))

    def setUp(self):
        self.client.force_login(_person("viewer", roles.Role.VIEWER))

    def test_the_audit_log_is_refused_and_shows_none_of_it(self):
        for url in (reverse("core:audit_list"), reverse("core:audit_detail", args=[self.event.pk])):
            with self.subTest(url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertNotContains(response, SECRET, status_code=403)

    def test_a_held_request_link_says_nothing_of_whether_its_entry_exists(self):
        answers = [
            self.client.get(reverse("core:approval_entry", kwargs={"approval_id": held})) for held in (HELD, uuid4())
        ]

        self.assertEqual([(answer.status_code, answer.get("Location")) for answer in answers], [(403, None)] * 2)

    def test_the_refusal_is_recorded_against_the_capability(self):
        self.client.get(reverse("core:audit_list"))

        denial = AuditLog.objects.filter(action=AuditLog.Action.DENIED).get()
        self.assertEqual(denial.metadata["capability"], Capability.READ_AUDIT_LOG)
        self.assertEqual(denial.metadata["actor"], "viewer")

    def test_a_page_read_under_read_opens(self):
        self.assertEqual(self.client.get(reverse("projects:list")).status_code, 200)

    def test_the_personal_calendar_is_refused(self):
        self.assertEqual(self.client.get(reverse("calendar:month")).status_code, 403)


class OperatorTests(TestCase):
    def test_an_operator_opens_the_audit_log_and_the_calendar(self):
        self.client.force_login(_person("operator", roles.Role.OPERATOR))

        for url in (reverse("core:audit_list"), reverse("calendar:month")):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_an_operator_follows_a_held_request_link_to_its_entry(self):
        self.client.force_login(_person("operator", roles.Role.OPERATOR))
        entry = AuditLog.objects.create(action=AuditLog.Action.CREATED, object_type="Approval", object_id=str(HELD))

        response = self.client.get(reverse("core:approval_entry", kwargs={"approval_id": HELD}))

        self.assertRedirects(response, reverse("core:audit_detail", args=[entry.pk]))


class NoRoleTests(TestCase):
    def test_a_person_without_a_role_opens_nothing_and_can_still_sign_out(self):
        self.client.force_login(_person("nobody", ""))

        self.assertEqual(self.client.get(reverse("projects:list")).status_code, 403)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)


class DeclarationTests(TestCase):
    def test_a_page_that_declares_nothing_reads_under_read(self):
        from hq.platform.application.pages import page_capability

        self.assertEqual(page_capability(View.as_view()), Capability.READ)

    def test_a_function_view_declares_through_capability_required(self):
        from hq.platform.application.pages import page_capability

        @capability_required("example.read")
        def page(_request):
            return HttpResponse()

        self.assertEqual(page_capability(page), "example.read")

    def test_a_page_reads_under_one_capability_never_several(self):
        from hq.platform.application.pages import page_capability

        class Several(View):
            required_capability = (Capability.READ, Capability.READ_AUDIT_LOG)

        with self.assertRaises(ImproperlyConfigured):
            page_capability(Several.as_view())


class _AuditPage(CapabilityRequiredMixin, View):
    required_capability = Capability.READ_AUDIT_LOG

    def get(self, _request):
        return HttpResponse(SECRET)


class _Unnamed(CapabilityRequiredMixin, View):
    def get(self, _request):
        return HttpResponse(SECRET)


@capability_required(Capability.READ_AUDIT_LOG)
def _audit_page(_request):
    return HttpResponse(SECRET)


def _gate(user, view):
    """What the page gate does with ``user`` at ``view``: None to let it run, or its refusal raised."""

    request = RequestFactory().get("/example/")
    request.user = user
    return LoginRequiredMiddleware(lambda _request: HttpResponse()).process_view(request, view, (), {})


class ExtensionViewTests(TestCase):
    """An extension's view, by the mixin or the decorator ``hq_sdk.web`` gives it, is refused by the one page gate."""

    def test_a_person_without_its_capability_is_refused_and_the_refusal_recorded(self):
        viewer = _person("viewer", roles.Role.VIEWER)

        for view in (_AuditPage.as_view(), _audit_page):
            with self.subTest(view=view.__name__), self.assertRaises(PermissionDenied):
                _gate(viewer, view)

        denials = AuditLog.objects.filter(action=AuditLog.Action.DENIED)
        self.assertEqual({denial.metadata["capability"] for denial in denials}, {Capability.READ_AUDIT_LOG})

    def test_a_person_holding_its_capability_is_let_through(self):
        operator = _person("operator", roles.Role.OPERATOR)

        for view in (_AuditPage.as_view(), _audit_page):
            with self.subTest(view=view.__name__):
                self.assertIsNone(_gate(operator, view))

    def test_a_mixin_view_that_names_no_capability_is_never_served(self):
        with self.assertRaises(ImproperlyConfigured):
            _gate(_person("operator", roles.Role.OPERATOR), _Unnamed.as_view())

    def test_a_test_holding_capabilities_is_answered_by_the_real_gate(self):
        with holding(Capability.READ_AUDIT_LOG):
            self.assertIsNone(_gate(_person("viewer", roles.Role.VIEWER), _AuditPage.as_view()))
        with holding(), self.assertRaises(PermissionDenied):
            _gate(_person("operator", roles.Role.OPERATOR), _AuditPage.as_view())
