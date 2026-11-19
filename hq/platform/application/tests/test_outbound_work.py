"""Outbound work an extension or the host declares, and what HQ derives from it.

Driven through the example plugin, composed as an extension is: its one
``OutboundWork`` declaration, its page, and the host's route, job, control,
capability and audit entries for it. The registry double does what a network
call does as far as the rule can tell (``hq_sdk.testing.reaches_out``), so a
request that reached it would be refused. The host's own work, offered to
nobody, is driven through what its units and timer ask for.
"""

import importlib
import os
import pkgutil
import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    modify_settings,
    override_settings,
    tag,
)
from django.urls import include, path, reverse
from django.utils import timezone

from hq.config.urls import urlpatterns as root_urlpatterns
from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.domains.jobs import runner
from hq.domains.jobs.models import HEARTBEAT_GRACE, Job
from hq.platform.application import host_work, outbound_work
from hq.platform.application.capabilities import capability_registry, execute_capability
from hq.platform.application.contracts import Effect
from hq.platform.application.integration_specs import requested
from hq.platform.application.markup_testing import elements
from hq.platform.application.outbound_work import OutboundWork, capability_for, due, validate
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.plugins import PluginIntegration, clear_plugin_composition_cache
from hq.platform.application.security import Principal, cli_principal, internal_principal
from hq.platform.application.tests.reads import reads
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AuditLog
from hq.platform.core.outbound import serving
from hq.platform.core.testing import operator
from hq.platform.core.tests.estate_sizes import assert_subject_cost_does_not_grow
from hq_sdk.outbound import Failed, ask, run_now
from hq_sdk.testing import OutboundInRequest, held_jobs, reaches_out
from tests.fixtures.example_hq_plugin import outbound as example, registry

EXAMPLE = "tests.fixtures.example_hq_plugin"
SCRIPT = {"X-Requested-With": "XMLHttpRequest"}


def calls_out(request):
    """What the rule exists to stop: a view asking the registry itself."""

    registry.read("first-note")
    return HttpResponse("never")


def entries_shown(page) -> list[str]:
    """The registry's entries as the example's page lists them, one item each."""

    return [
        item.words for item in elements(page.content.decode(), "li") if any(cell.tag == "td" for cell in item.ancestors)
    ]


urlpatterns = [
    *root_urlpatterns,
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
                "SEVERINO_HQ_PLUGINS": ",".join(part for part in (installed, f"{EXAMPLE}.plugin:plugin") if part),
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

        self.user = operator(self.client)
        self.page = reverse("example_plugin:index")
        self.route = reverse("jobs:ask", args=[example.LOOKUP])

    def press(self, subject="first-note", **extra):
        return self.client.post(self.route, form_keyed({"subject": subject}), **extra)


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
        self.assertEqual(entries_shown(page), ["alpha", "beta"])
        self.assertNotContains(page, "data-ask-status")
        ended = AuditLog.objects.filter(operation_id=str(job.pk), action=AuditLog.Action.DONE).get()
        self.assertEqual((ended.user, ended.metadata["operation"]), (self.user, example.LOOKUP))

    def test_without_script_the_post_returns_to_the_page_that_asked(self):
        with held_jobs():
            response = self.client.post(self.route, form_keyed({"subject": "first-note", "next": self.page}))

        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        self.assertEqual(Job.objects.get().state, "queued")

    def test_the_page_reaches_nothing_and_a_control_costs_the_same_however_often_it_ran(self):
        def arrange(runs):
            for _ in range(Job.objects.filter(kind=example.LOOKUP).count(), runs):
                with held_jobs() as held:
                    self.press(headers=SCRIPT)
                    held.run()
            self.registry.asked.clear()

        # A control is about its one piece of work, whatever its runs were.
        assert_subject_cost_does_not_grow(
            self, sizes=(1, 3), arrange=arrange, draw=lambda _runs: ask(example.LOOKUP, "first-note")
        )
        self.assertContains(self.client.get(self.page), "Look up")

        control = ask(example.LOOKUP, "first-note")
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
        self.assertEqual(entries_shown(page), ["alpha", "beta"])
        self.assertContains(page, "The registry lists nothing for this note.")
        self.assertEqual(ask(example.LOOKUP, "first-note").standing.state, "failed")
        self.assertEqual(ask(example.LOOKUP, "second-note").standing.state, "idle")
        audit = AuditLog.objects.filter(operation_id=str(failed.pk), action=AuditLog.Action.FAILED).get()
        self.assertEqual(audit.metadata["failure"]["message"], "The registry lists nothing for this note.")

    def test_a_fault_is_a_failed_job_named_by_its_kind(self):
        self.registry.listed = OSError("connection reset")
        with held_jobs() as held, self.assertLogs("severino.jobs", "ERROR"):
            self.press(headers=SCRIPT)
            held.run()

        job = Job.objects.get()
        self.assertEqual(job.state, "failed")
        note = ask(example.LOOKUP, "first-note").standing.note
        self.assertIn("OSError", note)
        self.assertNotIn("connection reset", note)

    def test_one_runs_at_a_time(self):
        with held_jobs() as held:
            first = self.press(headers=SCRIPT).json()
            again = self.press(headers=SCRIPT)
            other = self.press("second-note", headers=SCRIPT)

            # The same ask again follows the work already asked for.
            self.assertEqual((again.status_code, again.json()["status"]), (202, first["status"]))
            # Another subject is told, and nothing is queued behind the first.
            self.assertEqual((other.status_code, other.json()["state"]), (200, "failed"))
            self.assertEqual(other.json()["note"], outbound_work.ANOTHER_RUNNING)
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
        with held_jobs(), mock.patch("hq.platform.application.web_writes.web_principal", return_value=nobody):
            response = self.press(headers=SCRIPT)

        self.assertEqual(response.json()["state"], "failed")
        self.assertFalse(Job.objects.exists())

    def test_work_nobody_declared_has_no_route(self):
        response = self.client.post(reverse("jobs:ask", args=["example.undeclared"]), form_keyed())

        self.assertEqual(response.status_code, 404)


class TheCapabilityIsDerivedTests(ComposedExample):
    def test_the_declaration_is_a_capability_every_adapter_can_ask_through(self):
        spec = capability_registry()[example.LOOKUP]

        self.assertEqual((spec.effect, spec.target_kind, spec.target_label), ("remote_write", "key", "Note"))
        self.assertEqual(spec.required_capabilities, ("notes.write",))

    def test_a_machine_caller_is_answered_at_once(self):
        caller = Principal("example-agent", "api", frozenset({"notes.write"}))
        with held_jobs() as held:
            result = execute_capability(example.LOOKUP, keyed(), principal=caller, target="first-note")

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
        import hq_sdk

        lifting = {}
        for name in (info.name for info in pkgutil.iter_modules(hq_sdk.__path__)):
            module = importlib.import_module(f"hq_sdk.{name}")
            for attribute, value in vars(module).items():
                if getattr(value, "__module__", "") == "hq.platform.core.outbound":
                    lifting[f"hq_sdk.{name}.{attribute}"] = value

        # The error a refused call raises, for a test to name; nothing else.
        self.assertEqual(lifting, {"hq_sdk.testing.OutboundInRequest": OutboundInRequest})


class DeclarationTests(SimpleTestCase):
    def work(self, **changed):
        fields = {
            "name": "example.lookup.run",
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

    def test_an_extension_cannot_declare_work_the_host_declares(self):
        from hq.platform.application import outbound_work
        from hq.platform.application.projects import REFRESH

        integration = PluginIntegration(outbound=lambda: (self.work(name=REFRESH),))
        outbound_work.clear_outbound_work_cache()
        self.addCleanup(outbound_work.clear_outbound_work_cache)
        with (
            mock.patch("hq.platform.application.plugins.installed_integrations", return_value=((None, integration),)),
            self.assertRaisesMessage(ImproperlyConfigured, f"Duplicate outbound work {REFRESH!r}"),
        ):
            outbound_work.declared_work()

    def test_failed_is_a_sentence_and_not_a_fault(self):
        self.assertEqual(str(Failed("The registry lists nothing.")), "The registry lists nothing.")


class DeclaredWorkRunsOnItsCadenceTests(ComposedExample):
    """Work declared with ``every`` is done by the host's timer once it is due."""

    def setUp(self):
        super().setUp()
        # Only the example's declarations, whatever else the composition installs:
        # an installed extension's cadence is its own suite's to test.
        # The host's due work is here only as what the timer asks for.
        own = {work.name: work for work in example.work()}
        own["due"] = next(work for work in host_work.outbound() if work.name == "due")
        only = mock.patch("hq.platform.application.outbound_work.declared_work", return_value=own)
        only.start()
        self.addCleanup(only.stop)

    def done(self, ago, state=Job.State.SUCCEEDED):
        Job.objects.create(
            kind=example.REFRESH,
            label="Refresh",
            state=state,
            request={"subject": ""},
            finished_at=timezone.now() - ago,
        )

    def test_work_never_done_is_due_and_work_without_a_cadence_never_is(self):
        self.assertEqual([work.name for work in due()], [example.REFRESH])

    def test_work_done_within_its_cadence_is_not_due(self):
        self.done(timedelta(hours=2))

        self.assertEqual(due(), ())

    def test_work_last_done_longer_ago_than_its_cadence_is_due(self):
        self.done(timedelta(days=1, hours=1))

        self.assertEqual([work.name for work in due()], [example.REFRESH])

    def test_a_failed_attempt_is_not_the_work_done(self):
        self.done(timedelta(days=2))
        self.done(timedelta(hours=1), state=Job.State.FAILED)

        self.assertEqual([work.name for work in due()], [example.REFRESH])

    def test_work_being_done_is_not_due_again(self):
        Job.objects.create(kind=example.REFRESH, label="Refresh", state=Job.State.RUNNING, request={"subject": ""})

        self.assertEqual(due(), ())

    def test_the_timer_does_the_due_work_as_a_job_of_its_own(self):
        ended = outbound_work.run_for_host("due")

        job = Job.objects.get(kind=example.REFRESH)
        self.assertEqual((job.state, job.actor, job.result), ("succeeded", runner.SCHEDULER, {"seen": 4}))
        self.assertEqual(self.registry.asked, list(example.NOTES))
        self.assertEqual(Job.objects.get(kind="due").result["ended"], {example.REFRESH: "succeeded"})
        self.assertEqual(ended["state"], "succeeded")

        outbound_work.run_for_host("due")

        self.assertEqual(Job.objects.filter(kind=example.REFRESH).count(), 1)


class CadenceDeclarationTests(SimpleTestCase):
    def declared(self, **fields):
        return OutboundWork("example.cadence", "Cadence", "Example.", "notes.write", example.refresh, **fields)

    def test_work_about_one_record_cannot_be_due_on_a_cadence(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "about one record"):
            validate(self.declared(subject_label="Note", every=timedelta(days=1)))

    def test_a_cadence_is_a_positive_span_of_time(self):
        for every in (timedelta(0), timedelta(hours=-1), 3600):
            with self.subTest(every=every), self.assertRaisesMessage(ImproperlyConfigured, "positive timedelta"):
                validate(self.declared(every=every))

    def test_a_cadence_is_accepted_for_work_about_no_record(self):
        self.assertEqual(validate(self.declared(every=timedelta(days=1))).every, timedelta(days=1))


class JobWordingTests(ComposedExample):
    """What HQ says about a job is said in the words the work declares.

    A label names the work ("Refresh a project"), so a sentence with one as
    its subject reads wrong; the answer to an ask is the one requested form of
    its button's words. Every piece of work the composition declares, the
    host's and the example extension's, is driven through each thing HQ says
    of it: asked for, asked for while it runs, and its ending in the audit log.
    """

    def declared(self):
        works = outbound_work.declared_work().values()
        self.assertIn(example.LOOKUP, {work.name for work in works})
        return works

    def test_an_ask_is_answered_in_the_works_own_words(self):
        principal = internal_principal("test")
        for each in (work for work in self.declared() if work.offered):
            # Asked for as it is when nothing refuses it.
            work = replace(each, refuse=None)
            with self.subTest(work.name), held_jobs():
                started = outbound_work.request_work(work, "one", principal=principal)
                again = outbound_work.request_work(work, "one", principal=principal)
                other = outbound_work.request_work(work, "two", principal=principal)
                Job.objects.all().delete()
            self.assertEqual(started["message"], requested(work.words, "one"))
            for answer in (again, other):
                self.assertNotIn(work.label, answer["message"])
            self.assertTrue(started["started"])
            self.assertFalse(again["started"] or other["started"])

    def test_the_hosts_own_work_already_running_is_answered_without_its_label(self):
        for work in outbound_work.host_work():
            with self.subTest(work.name):
                Job.objects.create(kind=work.name, label=work.label, state=Job.State.RUNNING)
                answer = outbound_work.run_for_host(work.name)
                Job.objects.all().delete()
                self.assertEqual(answer["state"], "running")
                self.assertNotIn(work.label, answer["note"])

    def test_the_audit_log_titles_an_ending_with_the_label(self):
        labels = [work.label for work in self.declared()]
        for label in labels:
            with self.subTest(label):
                job = runner.run("example.wording", label, lambda progress: {})
                ended = AuditLog.objects.get(operation_id=str(job.pk), action=AuditLog.Action.DONE)
                self.assertEqual(ended.object_repr, label)


UNITS = Path(settings.BASE_DIR) / "deploy" / "systemd"
INSTANCE = re.compile(r"^(?:Unit|OnFailure)=severino-hq-job@(?P<name>.+)\.service$", re.MULTILINE)


@contextmanager
def host_declares(*works: OutboundWork):
    """The host's own work as ``works``, beside whatever the composition installs."""

    outbound_work.clear_outbound_work_cache()
    try:
        with mock.patch.object(host_work, "outbound", return_value=works):
            yield
    finally:
        outbound_work.clear_outbound_work_cache()


def own(name="example.work", run=lambda progress, *, subject, principal: {}, every=None) -> OutboundWork:
    return OutboundWork(name, "Example work", "", "", run, every=every, offered=False)


class HostWorkTests(TestCase):
    """The host's own work: done as a job for a unit that names it, and
    offered to nobody else."""

    def test_work_is_done_as_a_job_and_answered_as_it_ended(self):
        with host_declares(own(run=lambda progress, *, subject, principal: {"read": 3})):
            answer = outbound_work.run_for_host("example.work")
        job = Job.objects.get(pk=answer["job"])
        self.assertEqual((answer["name"], answer["state"]), ("example.work", "succeeded"))
        self.assertEqual((job.kind, job.actor, job.result), ("example.work", runner.SCHEDULER, {"read": 3}))

    def test_work_that_says_why_it_failed_is_answered_with_the_sentence(self):
        def work(progress, *, subject, principal):
            raise Failed("The index did not answer.")

        with host_declares(own(run=work)):
            answer = outbound_work.run_for_host("example.work")
        self.assertEqual((answer["state"], answer["note"]), ("failed", "The index did not answer."))

    def test_work_already_under_way_is_not_done_twice(self):
        Job.objects.create(kind="example.work", label="Example work", state=Job.State.RUNNING)
        work = mock.Mock()
        with host_declares(own(run=lambda progress, *, subject, principal: work())):
            answer = outbound_work.run_for_host("example.work")
        work.assert_not_called()
        self.assertEqual(answer["state"], "running")
        self.assertNotIn("job", answer)

    def test_a_unit_can_name_only_the_hosts_own_work(self):
        """The controller cannot have HQ run work by name that the host did not
        declare for it: an unknown name, an extension's, or work offered as a
        capability, which its own authorization guards."""

        from hq.platform.application.projects import REFRESH

        extension = PluginIntegration(outbound=lambda: (own("example.theirs"),))
        with (
            host_declares(own()),
            mock.patch("hq.platform.application.plugins.installed_integrations", return_value=((None, extension),)),
        ):
            self.assertIn("example.theirs", outbound_work.declared_work())
            for name in ("example.other", "example.theirs", REFRESH):
                with self.subTest(name), self.assertRaisesMessage(ValueError, f"No host work named {name!r}"):
                    outbound_work.run_for_host(name)
                with self.subTest(name), self.assertRaisesMessage(ValueError, f"No host work named {name!r}"):
                    outbound_work.start_for_host(name)
        self.assertFalse(Job.objects.exists())

    def test_the_bridge_answers_for_it(self):
        action = next(action for action in ACTIONS if action.name == "job")
        with host_declares(own()):
            answer = action.run({"name": "example.work"}, None)
        self.assertEqual(answer["state"], "succeeded")

    def test_it_is_offered_to_nobody(self):
        with host_declares(own()):
            self.assertNotIn("example.work", {spec.name for spec in outbound_work.capability_specs()})
            self.assertNotIn("example.work", capability_registry())
            with self.assertRaises(LookupError):
                ask("example.work")
            with self.assertRaises(ImproperlyConfigured):
                capability_for(own())
            operator(self.client)
            response = self.client.post(reverse("jobs:ask", args=["example.work"]), form_keyed({}))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Job.objects.exists())

    def test_it_needs_no_capability_of_its_own(self):
        self.assertFalse(validate(replace(own(), effect=Effect.READ)).offered)
        with self.assertRaises(ImproperlyConfigured):
            validate(replace(own(), effect=Effect.READ, offered=True))


class StartForHostTests(TransactionTestCase):
    def test_hq_starts_work_on_a_thread_of_its_own(self):
        with host_declares(own()), mock.patch("hq.domains.jobs.runner.threading.Thread") as thread:
            self.assertTrue(outbound_work.start_for_host("example.work"))
        thread.return_value.start.assert_called_once_with()
        self.assertEqual(Job.objects.get().state, Job.State.QUEUED)

    def test_starting_what_is_already_live_is_no(self):
        Job.objects.create(kind="example.work", label="Example work", state=Job.State.QUEUED)
        with host_declares(own()):
            self.assertFalse(outbound_work.start_for_host("example.work"))


@tag("source", "tree")
@reads("deploy/systemd/**")
class ShippedUnitTests(TestCase):
    """The shipped units and the declarations name the same work: a timer
    starts it on a schedule, or a failed unit starts it through OnFailure."""

    def asked_for(self) -> set[str]:
        return {
            match["name"]
            for unit in UNITS.rglob("*")
            if unit.is_file() and not unit.name.endswith(".example")
            for match in INSTANCE.finditer(unit.read_text())
        }

    def test_the_units_ask_for_exactly_the_hosts_work_without_a_cadence(self):
        self.assertEqual(self.asked_for(), {work.name for work in outbound_work.host_work() if work.every is None})

    def test_work_with_a_cadence_is_asked_for_by_no_unit_of_its_own(self):
        clocked = {work.name for work in outbound_work.host_work() if work.every is not None}
        self.assertTrue(clocked)
        self.assertFalse(self.asked_for() & clocked)

    def test_each_timer_starts_work_of_its_own(self):
        started = [match["name"] for timer in UNITS.glob("*.timer") for match in INSTANCE.finditer(timer.read_text())]
        self.assertEqual(len(started), len(set(started)))

    def test_nothing_shipped_starts_a_process_in_the_web_container_for_it(self):
        for unit in UNITS.glob("*.service"):
            with self.subTest(unit=unit.name):
                self.assertNotIn("manage.py", unit.read_text())


class UnitFailureTests(TestCase):
    """A failed unit asks for the machine's units to be read, and for nothing else."""

    def test_the_read_is_asked_for_and_the_doorbell_rung(self):
        from hq.domains.control_plane.models import ReadRequest
        from hq.domains.control_plane.observations.host import UNIT_KIND

        from .. import cadence

        with (
            mock.patch.object(cadence, "_ring", return_value=True) as doorbell,
            self.captureOnCommitCallbacks(execute=True),
        ):
            answer = outbound_work.run_for_host("units.read")

        self.assertEqual(answer["state"], "succeeded")
        doorbell.assert_called_once_with("controller")
        self.assertEqual(list(ReadRequest.objects.values_list("connection_ref", "kind")), [("", UNIT_KIND)])
        self.assertEqual(Job.objects.get(pk=answer["job"]).result, {"asked": UNIT_KIND})
        # The sweep it causes reads that kind alone, whatever the cadence says.
        with mock.patch.object(cadence, "sweep_interval", return_value=cadence.slowest_sweep_interval()):
            from hq.platform.application.inventory_testing import store

            store("host.firewall", {"record": "interface-binding"})
            verdict = cadence.sweep_due("example-controller")
        self.assertEqual((verdict["due"], verdict["only_kinds"]), (True, [UNIT_KIND]))

    def test_a_doorbell_that_cannot_be_rung_leaves_the_request_for_the_timer(self):
        from hq.domains.control_plane.models import ReadRequest

        from .. import cadence

        with mock.patch.object(cadence, "_ring", return_value=False), self.captureOnCommitCallbacks(execute=True):
            answer = outbound_work.run_for_host("units.read")

        self.assertEqual(answer["state"], "succeeded")
        self.assertEqual(ReadRequest.objects.count(), 1)


class HostDueTests(TestCase):
    """The ``due`` timer does the host's work whose cadence has come round, in
    the one loop that does the extensions'."""

    def setUp(self):
        self.run = mock.Mock(return_value={"read": 1})
        due_work = next(work for work in host_work.outbound() if work.name == "due")
        self.enterContext(
            host_declares(
                due_work,
                own(run=lambda progress, *, subject, principal: self.run(), every=timedelta(days=1)),
                own("example.asked", run=lambda progress, *, subject, principal: self.run()),
            )
        )
        # Only these come due: an installed extension's cadence is its own
        # suite's, and its capabilities stay declared.
        mine = {work.name for work in outbound_work.host_work()}
        every_due = outbound_work.due
        self.enterContext(
            mock.patch.object(
                outbound_work,
                "due",
                side_effect=lambda now=None: tuple(work for work in every_due(now) if work.name in mine),
            )
        )

    def test_work_never_done_is_done_and_work_a_unit_asks_for_is_not(self):
        answer = outbound_work.run_for_host("due")

        self.run.assert_called_once()
        self.assertEqual(
            Job.objects.get(kind="due").result,
            {"due": 1, "ended": {"example.work": "succeeded"}, "lost": 0, "lapsed": 0},
        )
        self.assertEqual(answer["state"], "succeeded")

    def test_a_job_whose_process_stopped_is_marked_lost(self):
        stale = timezone.now() - HEARTBEAT_GRACE - timedelta(minutes=1)
        gone = Job.objects.create(
            kind="example.gone", label="Gone", state=Job.State.RUNNING, started_at=stale, heartbeat_at=stale
        )
        outbound_work.run_for_host("due")

        gone.refresh_from_db()
        self.assertEqual(gone.state, Job.State.LOST)
        self.assertEqual(Job.objects.get(kind="due").result["lost"], 1)

    def test_work_done_within_its_cadence_is_left_alone(self):
        Job.objects.create(
            kind="example.work",
            label="Example work",
            state=Job.State.SUCCEEDED,
            request={"subject": ""},
            finished_at=timezone.now() - timedelta(hours=3),
        )
        outbound_work.run_for_host("due")

        self.run.assert_not_called()


class HostCadenceTests(TestCase):
    def test_the_hosts_own_work_declares_how_often_it_is_done(self):
        every = {work.name: work.every for work in outbound_work.host_work()}

        self.assertEqual(
            {every[name] for name in ("audit.prune", "content.sync", "registry.refresh", "sessions.clear")},
            {timedelta(days=1)},
        )
        self.assertIsNone(every["units.read"])


class CadenceDueTests(TestCase):
    """When work is due: one check, shared by the host's and the extensions' work."""

    def done(self, ago, state=Job.State.SUCCEEDED, request=None):
        Job.objects.create(
            kind="example.work", label="Example", state=state, request=request or {}, finished_at=timezone.now() - ago
        )

    def due(self, every=timedelta(hours=1), **kwargs):
        return runner.due("example.work", every, now=timezone.now(), **kwargs)

    def test_a_timer_a_few_minutes_early_still_finds_hourly_work_due(self):
        self.done(timedelta(minutes=55))

        self.assertTrue(self.due())

    def test_work_done_well_within_its_cadence_is_not_due(self):
        self.done(timedelta(minutes=30))

        self.assertFalse(self.due())

    def test_a_failure_is_not_the_work_done(self):
        self.done(timedelta(minutes=5), state=Job.State.FAILED)

        self.assertTrue(self.due())

    def test_a_subject_narrows_to_the_work_about_it(self):
        self.done(timedelta(minutes=5), request={"subject": "other"})

        self.assertTrue(self.due(subject=""))
        self.assertFalse(self.due(subject="other"))
