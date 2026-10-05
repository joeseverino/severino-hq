"""Outbound work an extension declares, and what HQ derives from it.

Driven through the example plugin, composed as an extension is: its one
``OutboundWork`` declaration, its page, and the host's route, job, control,
capability and audit entries for it. The registry double does what a network
call does as far as the rule can tell (``hq_sdk.testing.reaches_out``), so a
request that reached it would be refused.
"""

from __future__ import annotations

import importlib
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, modify_settings, override_settings
from django.urls import include, path, reverse

from hq.config.urls import urlpatterns as host_urlpatterns
from hq.domains.jobs.models import Job
from hq.platform.application.capabilities import capability_registry, execute_capability
from hq.platform.application.outbound_work import OutboundWork, capability_for, validate
from hq.platform.application.plugins import PluginIntegration, clear_plugin_composition_cache
from hq.platform.application.security import Principal, cli_principal
from hq.platform.core.models import AuditLog
from hq.platform.core.outbound import serving
from hq_sdk.outbound import Failed, ask, run_now
from hq_sdk.testing import OutboundInRequest, held_jobs, reaches_out
from tests.fixtures.example_hq_plugin import outbound as example
from tests.fixtures.example_hq_plugin import registry

EXAMPLE = "tests.fixtures.example_hq_plugin"
SCRIPT = {"X-Requested-With": "XMLHttpRequest"}



def calls_out(request):
    """What the rule exists to stop: a view asking the registry itself."""

    registry.read("first-note")
    return HttpResponse("never")


urlpatterns = [
    *host_urlpatterns,
    path("examples/notes/", include(f"{EXAMPLE}.urls")),
    path("examples/inline/", calls_out),
]


class Registry:
    """The registry, as far as the example can tell: it answers, and reaching
    it is a network call."""

    def __init__(self, listed=("alpha", "beta")):
        self.listed = listed
        self.asked: list[str] = []

    def read(self, slug):
        reaches_out()
        self.asked.append(slug)
        if isinstance(self.listed, Exception):
            raise self.listed
        return list(self.listed) if self.listed else None


@override_settings(ROOT_URLCONF=__name__)
@modify_settings(INSTALLED_APPS={"append": EXAMPLE})
class ComposedExample(TestCase):
    """The example plugin installed beside whatever the suite already loads."""

    def setUp(self):
        super().setUp()
        installed = os.environ.get("SEVERINO_HQ_PLUGINS", "")
        composed = mock.patch.dict(
            os.environ,
            {
                "SEVERINO_HQ_PLUGINS": ",".join(
                    part for part in (installed, f"{EXAMPLE}.plugin:plugin") if part
                ),
                # A fixture has no signed approval to be admitted by.
                "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
            },
        )
        composed.start()
        self.addCleanup(clear_plugin_composition_cache)
        self.addCleanup(composed.stop)
        clear_plugin_composition_cache()

        self.registry = Registry()
        reading = mock.patch.object(registry, "read", self.registry.read)
        reading.start()
        self.addCleanup(reading.stop)

        self.user = get_user_model().objects.create_user("operator", password="x")
        self.client.force_login(self.user)
        self.page = reverse("example_plugin:index")
        self.route = reverse("jobs:ask", args=[example.LOOKUP])

    def press(self, subject="first-note", **extra):
        return self.client.post(self.route, {"subject": subject}, **extra)


class AnExtensionActionAnswersAtOnceTests(ComposedExample):
    def test_the_request_answers_and_the_work_runs_afterwards(self):
        with held_jobs() as held:
            response = self.press(headers=SCRIPT)

            answer = response.json()
            job = Job.objects.get(kind=example.LOOKUP)
            self.assertEqual((response.status_code, answer["state"], answer["live"]), (202, "queued", True))
            self.assertEqual(answer["status"], reverse("jobs:status", args=[job.pk]))
            self.assertEqual((job.state, job.started_at, job.result), ("queued", None, {}))
            self.assertEqual((job.requested_by, job.request), (self.user, {"subject": "first-note"}))
            # Nothing reached the registry: the request only recorded the ask.
            self.assertEqual(self.registry.asked, [])
            self.assertTrue(self.client.get(answer["status"]).json()["live"])
            # A page loaded while the work is live resumes following it.
            self.assertContains(self.client.get(self.page), f'data-ask-status="{answer["status"]}"')

            held.run()

        self.assertEqual(self.registry.asked, ["first-note"])
        job.refresh_from_db()
        self.assertEqual((job.state, job.result["entries"]), ("succeeded", ["alpha", "beta"]))
        standing = self.client.get(reverse("jobs:status", args=[job.pk])).json()
        self.assertEqual((standing["state"], standing["note"]), ("done", "The registry lists 2 entries."))
        page = self.client.get(self.page)
        self.assertContains(page, "alpha, beta")
        self.assertNotContains(page, "data-ask-status")
        ended = AuditLog.objects.filter(operation_id=str(job.pk), action=AuditLog.Action.IMPORTED).get()
        self.assertEqual((ended.user, ended.metadata["kind"]), (self.user, example.LOOKUP))

    def test_without_script_the_post_returns_to_the_page_that_asked(self):
        with held_jobs():
            response = self.client.post(self.route, {"subject": "first-note", "next": self.page})

        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        self.assertEqual(Job.objects.get().state, "queued")

    def test_the_page_reaches_nothing_and_costs_one_query_a_control(self):
        with held_jobs() as held:
            self.press(headers=SCRIPT)
            held.run()
        self.registry.asked.clear()

        with self.assertNumQueries(1):
            control = ask(example.LOOKUP, "first-note")
        self.assertContains(self.client.get(self.page), "Look up")

        self.assertEqual((control.standing.state, control.url, control.value), ("idle", self.route, "first-note"))
        self.assertEqual(self.registry.asked, [])

    def test_a_failure_says_why_and_leaves_what_was_stored(self):
        with held_jobs() as held:
            self.press(headers=SCRIPT)
            held.run()
            self.registry.listed = ()
            self.press(headers=SCRIPT)
            held.run()

        failed = Job.objects.filter(state="failed").get()
        self.assertEqual(failed.error, "The registry lists nothing for this note.")
        page = self.client.get(self.page)
        # The last good reading is still what the page shows, beside the reason.
        self.assertContains(page, "alpha, beta")
        self.assertContains(page, "The registry lists nothing for this note.")
        self.assertEqual(ask(example.LOOKUP, "first-note").standing.state, "failed")
        self.assertEqual(ask(example.LOOKUP, "second-note").standing.state, "idle")
        audit = AuditLog.objects.filter(operation_id=str(failed.pk), action=AuditLog.Action.FAILED).get()
        self.assertEqual(audit.metadata["failure"]["message"], "The registry lists nothing for this note.")

    def test_a_fault_is_a_failed_job_with_its_traceback(self):
        self.registry.listed = OSError("connection reset")
        with held_jobs() as held:
            self.press(headers=SCRIPT)
            held.run()

        job = Job.objects.get()
        self.assertEqual(job.state, "failed")
        self.assertIn("Traceback", job.error)
        self.assertIn("connection reset", ask(example.LOOKUP, "first-note").standing.note)

    def test_one_runs_at_a_time(self):
        with held_jobs() as held:
            first = self.press(headers=SCRIPT).json()
            again = self.press(headers=SCRIPT)
            other = self.press("second-note", headers=SCRIPT)

            # The same ask again follows the work already asked for.
            self.assertEqual((again.status_code, again.json()["status"]), (202, first["status"]))
            # Another subject is told, and nothing is queued behind the first.
            self.assertEqual((other.status_code, other.json()["state"]), (200, "failed"))
            self.assertEqual(other.json()["note"], "Look up is already running.")
            self.assertEqual(Job.objects.count(), 1)
            held.run()

        self.assertEqual(self.registry.asked, ["first-note"])

    def test_work_that_cannot_be_asked_for_says_so_before_any_job(self):
        control = ask(example.LOOKUP, example.ARCHIVED)
        self.assertEqual((control.disabled, control.title), (True, "An archived note is not looked up."))

        with held_jobs() as held:
            response = self.press(example.ARCHIVED, headers=SCRIPT)

        self.assertEqual((response.status_code, response.json()["state"]), (200, "failed"))
        self.assertEqual(response.json()["note"], "An archived note is not looked up.")
        self.assertEqual((Job.objects.count(), held.waiting), (0, []))

    def test_an_operator_without_the_capability_is_refused(self):
        nobody = Principal("operator", "web", frozenset())
        with held_jobs(), mock.patch("hq.platform.application.security.web_principal", return_value=nobody):
            response = self.press(headers=SCRIPT)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Job.objects.exists())

    def test_work_nobody_declared_has_no_route(self):
        response = self.client.post(reverse("jobs:ask", args=["example.undeclared"]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(self.route).status_code, 405)


class TheCapabilityIsDerivedTests(ComposedExample):
    def test_the_declaration_is_a_capability_every_adapter_can_ask_through(self):
        spec = capability_registry()[example.LOOKUP]

        self.assertEqual((spec.effect, spec.target_kind, spec.target_label), ("remote_write", "key", "Note"))
        self.assertEqual(spec.required_capabilities, ("notes.write",))

    def test_a_machine_caller_is_answered_at_once(self):
        caller = Principal("example-agent", "api", frozenset({"notes.write"}))
        with held_jobs() as held:
            result = execute_capability(example.LOOKUP, {}, principal=caller, target="first-note")

            self.assertEqual((result["ok"], result["started"]), (True, True))
            self.assertEqual(Job.objects.get(pk=result["job"]).actor, "example-agent")
            self.assertEqual(self.registry.asked, [])
            held.run()

        self.assertEqual(self.registry.asked, ["first-note"])

    def test_a_caller_without_the_capability_is_denied(self):
        caller = Principal("example-agent", "api", frozenset({"notes.read"}))

        result = execute_capability(example.LOOKUP, {}, principal=caller, target="first-note")

        self.assertEqual((result["ok"], result["error"]["code"]), (False, "forbidden"))
        self.assertFalse(Job.objects.exists())

    def test_a_command_line_waits_for_the_work_because_nothing_outlives_it(self):
        result = execute_capability(example.LOOKUP, {}, principal=cli_principal(), target="first-note")

        self.assertEqual((result["ok"], result["result"]["seen"]), (True, 2))
        self.assertEqual(Job.objects.get().state, "succeeded")

        job = run_now(example.LOOKUP, "second-note", principal=cli_principal())
        self.assertEqual((job.state, self.registry.asked), ("succeeded", ["first-note", "second-note"]))


class NothingLiftsTheRuleTests(ComposedExample):
    """An extension's work reaches out only where HQ runs it off the request."""

    def test_work_run_inside_a_request_is_refused(self):
        request = RequestFactory().post("/examples/notes/")

        with serving(request):
            with self.assertRaises(OutboundInRequest):
                run_now(example.LOOKUP, "first-note", principal=cli_principal())
            with self.assertRaises(OutboundInRequest):
                example.look_up(lambda *said, **how: None, subject="first-note", principal=None)
            with held_jobs() as held:
                self.press(headers=SCRIPT)
                with self.assertRaises(OutboundInRequest):
                    held.run()

        self.assertEqual(self.registry.asked, [])

    def test_a_view_that_calls_out_itself_is_refused_before_the_call_leaves(self):
        with self.assertRaises(OutboundInRequest):
            self.client.get("/examples/inline/")

        self.assertEqual(self.registry.asked, [])

    def test_the_sdk_exports_nothing_that_enters_an_exception_or_leaves_a_request(self):
        from hq_sdk.contract import module_names

        lifting = {}
        for name in (*module_names(), "contract"):
            module = importlib.import_module(f"hq_sdk.{name}")
            for attribute, value in vars(module).items():
                if getattr(value, "__module__", "") == "hq.platform.core.outbound":
                    lifting[f"hq_sdk.{name}.{attribute}"] = value

        # The error a refused call raises, for a test to name; nothing else.
        self.assertEqual(lifting, {"hq_sdk.testing.OutboundInRequest": OutboundInRequest})


class DeclarationTests(SimpleTestCase):
    def work(self, **changed):
        fields = {
            "name": "example.lookup",
            "label": "Look up",
            "summary": "Ask the registry.",
            "required_capability": "notes.write",
            "run": lambda progress, *, subject, principal: {},
        }
        return OutboundWork(**{**fields, **changed})

    def test_a_declaration_hq_could_not_run_fails_when_the_composition_loads(self):
        for changed in (
            {"name": "Not A Name"},
            {"name": "example." + "x" * 64},
            {"label": ""},
            {"label": "x" * 61},
            {"effect": "read"},
            {"refuse": "never"},
            {"run": lambda progress: {}},
        ):
            with self.subTest(changed), self.assertRaises(ImproperlyConfigured):
                validate(self.work(**changed))
        with self.assertRaises(ImproperlyConfigured):
            validate(object())

    def test_work_about_nothing_in_particular_takes_no_target(self):
        spec = capability_for(self.work())

        self.assertIsNone(spec.target_kind)
        self.assertEqual(capability_for(self.work(subject_label="Note")).target_kind, "key")

    def test_two_declarations_of_one_name_fail(self):
        from hq.platform.application import outbound_work

        integration = PluginIntegration(outbound=lambda: (self.work(), self.work()))
        outbound_work.clear_outbound_work_cache()
        self.addCleanup(outbound_work.clear_outbound_work_cache)
        with (
            mock.patch("hq.platform.application.plugins.installed_integrations", return_value=((None, integration),)),
            self.assertRaisesMessage(ImproperlyConfigured, "Duplicate outbound work"),
        ):
            outbound_work.declared_work()

    def test_failed_is_a_sentence_and_not_a_fault(self):
        self.assertEqual(str(Failed("The registry lists nothing.")), "The registry lists nothing.")
