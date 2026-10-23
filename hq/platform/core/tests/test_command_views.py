"""Browser execution tests for the registry-driven command page."""

import json
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project
from hq.platform.api.models import IdempotencyRecord
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_targets import capability_target_options
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tests.markup import elements, links, select, text, texts, with_class, within
from hq.platform.application.ui import MISSING
from hq.platform.core.models import AuditLog
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow

User = get_user_model()


def fields(page) -> set[str]:
    return {element.attrs["name"] for element in select(page, "name") if element.tag in {"input", "select", "textarea"}}


def submits(page):
    return [element for element in within(page, "data-command-form") if element.attrs.get("type") == "submit"]


def options(page) -> list[str]:
    return [option.attrs["value"] for option in elements(page, "option")]


def chosen_options(page) -> list[str]:
    """The target's chosen option."""

    return [option.attrs["value"] for option in within(page, "name", "__target") if "selected" in option.attrs]


def result_title(page) -> str | None:
    found = select(page, "id", "command-result-title")
    return found[0].words if found else None


class CommandViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="command-operator",
            email="operator@example.test",
            password="strongtestpass-1234",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _create_payload(self, *, name="Browser command", slug="browser-command"):
        response = self.client.get("/commands/project.create/")
        return {
            "name": name,
            "slug": slug,
            "__execution_key": response.context["form"].initial["__execution_key"],
        }

    def test_unknown_command_is_404(self):
        response = self.client.get("/commands/example.missing/")

        self.assertEqual(response.status_code, 404)

    def test_authority_is_checked_before_the_schema_is_built(self):
        principal = Principal("reader", "web", frozenset({Capability.READ}))
        with (
            mock.patch("hq.platform.core.command_views.web_principal", return_value=principal),
            mock.patch("hq.platform.core.command_views.command_form_class") as form_factory,
        ):
            response = self.client.get("/commands/project.create/")

        self.assertEqual(response.status_code, 403)
        form_factory.assert_not_called()

    def test_form_and_machine_contract_come_from_the_registered_schema(self):
        response = self.client.get("/commands/project.create/")

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual({"name", "slug", "__execution_key"}, fields(response))
        self.assertNotIn("__target", fields(response))

    def test_the_page_is_a_title_what_it_changes_and_one_button(self):
        response = self.client.get("/commands/project.create/")

        self.assertEqual([button.words for button in submits(response)], ["Create project"])
        self.assertNotIn("project.create", text(response))
        for built in (
            "manage_projects",
            "Handler",
            "JSON Schema",
            "Strict input",
            "Run command",
            "Preview",
            "additionalProperties",
        ):
            self.assertNotContains(response, built)

    def test_a_lede_says_what_the_title_does_not(self):
        restated = self.client.get("/commands/project.create/")
        declared = self.client.get("/commands/certificate.renew/")

        self.assertFalse(with_class(restated, "page-lede"))
        self.assertIn(capability_registry()["certificate.renew"].summary, text(declared))

    def test_the_trail_names_where_the_form_lives(self):
        response = self.client.get("/commands/project.create/")

        trail = [(link.words, link.attrs["href"]) for link in within(response, "class", "page-trail")]
        self.assertEqual(trail, [("Projects", "/projects/")])

    def test_a_field_reads_as_its_model_declares_it(self):
        response = self.client.get("/commands/project.create/")

        (status,) = select(response, "name", "status")
        self.assertEqual(status.tag, "select")
        self.assertIn(Project.Status.ACTIVE.label, [option.words for option in within(response, "name", "status")])
        (notes,) = select(response, "name", "notes")
        self.assertEqual(notes.tag, "textarea")

    def test_a_page_opened_for_one_thing_names_it_instead_of_listing_every_other(self):
        ManagedResource.objects.create(key="example-certificate", kind="tls.certificate", spec={})
        ManagedResource.objects.create(key="example-other", kind="tls.certificate", spec={})

        chosen = self.client.get("/commands/certificate.renew/", {"target": "example-certificate"})
        open_ = self.client.get("/commands/certificate.renew/")

        (target,) = select(chosen, "name", "__target")
        self.assertIn("hidden", target.attrs)
        self.assertEqual(chosen_options(chosen), ["example-certificate"])
        self.assertIn("example-certificate", text(chosen))
        self.assertIn(("Change", "/commands/certificate.renew/"), links(chosen))
        (target,) = select(open_, "name", "__target")
        self.assertNotIn("hidden", target.attrs)
        self.assertIn("Choose one…", text(open_))

    def test_targeted_command_derives_its_target_control(self):
        Project.objects.create(name="HQ", slug="hq")
        response = self.client.get("/commands/project.update/", {"target": "hq"})

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual({"__target", "__expected_updated_at"}, fields(response))
        self.assertEqual(chosen_options(response), ["hq"])

    def test_retry_keys_are_automatic_and_target_language_is_operator_facing(self):
        ManagedResource.objects.create(
            key="example-certificate",
            kind="tls.certificate",
            spec={},
        )
        ManagedResource.objects.create(
            key="example-zone",
            kind="cloudflare.zone",
            spec={},
        )
        response = self.client.get("/commands/certificate.renew/")

        (label,) = select(response, "for", "id___target")
        self.assertEqual(label.words, "Certificate")
        self.assertIn("The certificate to renew.", text(response))
        self.assertIn("example-certificate", options(response))
        self.assertNotIn("example-zone", options(response))
        (key,) = select(response, "name", "idempotency_key")
        self.assertEqual(key.attrs["type"], "hidden")
        self.assertNotIn("Idempotency Key", text(response))
        self.assertNotContains(response, "request_certificate_renewal")
        self.assertNotContains(response, "provider work runs outside this page request")

    def test_target_choices_cost_the_same_however_many_targets(self):
        principal = Principal(
            "operator",
            "web",
            frozenset({Capability.READ, Capability.REQUEST_CERTIFICATE_RENEWAL}),
        )

        def arrange(count):
            for index in range(ManagedResource.objects.count(), count):
                ManagedResource.objects.create(key=f"example-certificate-{index}", kind="tls.certificate", spec={})

        assert_cost_does_not_grow(
            self,
            sizes=(1, 5),
            arrange=arrange,
            draw=lambda _count: capability_target_options(
                capability_registry()["certificate.renew"], principal=principal
            ),
            rows=lambda options: len(options or ()),
        )

    def test_discovery_context_filters_and_explains_infrastructure_targets(self):
        ManagedResource.objects.create(
            key="example-device",
            kind="tailscale.device",
            spec={},
        )
        ManagedResource.objects.create(
            key="example-zone",
            kind="cloudflare.zone",
            spec={},
        )

        response = self.client.get("/commands/infrastructure.reconcile/", {"kind": "tailscale.device"})

        self.assertIn("example-device", options(response))
        self.assertNotIn("example-zone", options(response))

    def test_update_hydrates_the_selected_resource_and_concurrency_guard(self):
        resource = ManagedResource.objects.create(
            key="example-record",
            kind="cloudflare.dns_record",
            spec={"name": "www.example.test", "type": "A", "content": "192.0.2.1"},
            enabled=True,
        )

        response = self.client.get(
            "/commands/infrastructure.resource.update/",
            {"kind": "cloudflare.dns_record", "target": resource.key},
        )
        form = response.context["form"]

        self.assertEqual(form["__target"].value(), resource.key)
        self.assertEqual(form["key"].value(), resource.key)
        self.assertEqual(form["kind"].value(), resource.kind)
        self.assertEqual(json.loads(form["spec"].value()), resource.spec)
        self.assertTrue(form["enabled"].value())
        self.assertEqual(form["__expected_updated_at"].value(), resource.updated_at.isoformat())
        self.assertContains(response, "data-command-hydrate-target")

    def test_success_uses_prg_and_the_application_audit_boundary(self):
        response = self.client.post("/commands/project.create/", self._create_payload(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(result_title(response), "Done")
        self.assertTrue(Project.objects.filter(slug="browser-command").exists())
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
        audit = AuditLog.objects.get(object_type="Project")
        self.assertEqual(audit.metadata["interface"], "web")
        self.assertEqual(audit.metadata["operation"], "project.create")

    def test_a_contextual_command_returns_to_the_workflow_that_offered_it(self):
        source = "/infrastructure/findings/"
        form = self.client.get("/commands/project.create/", {"next": source}).context["form"]
        payload = {
            "name": "Contextual command",
            "slug": "contextual-command",
            "__execution_key": form.initial["__execution_key"],
            "next": form.initial["next"],
        }

        response = self.client.post("/commands/project.create/", payload, follow=True)

        self.assertIn(("Back", source), links(response))

    def test_a_command_never_returns_to_an_external_site(self):
        response = self.client.get("/commands/project.create/", {"next": "https://attacker.example/"})

        self.assertEqual(response.context["form"].initial["next"], "")
        self.assertEqual(response.context["return_url"], "")

    def test_exact_retry_replays_one_committed_write(self):
        payload = self._create_payload(slug="safe-retry")

        first = self.client.post("/commands/project.create/", payload)
        second = self.client.post("/commands/project.create/", payload, follow=True)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "This was already done earlier. Nothing ran again.")
        self.assertEqual(Project.objects.filter(slug="safe-retry").count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)

    def test_execution_key_cannot_be_reused_for_different_input(self):
        payload = self._create_payload(slug="first-request")
        self.client.post("/commands/project.create/", payload)

        response = self.client.post(
            "/commands/project.create/",
            {**payload, "name": "Changed", "slug": "changed-request"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "already used for a different request", status_code=409)
        self.assertFalse(Project.objects.filter(slug="changed-request").exists())

    def test_a_refused_command_keeps_nothing_under_its_key(self):
        """The same key runs once what refused it has gone."""

        taken = Project.objects.create(name="Taken", slug="taken-slug")
        payload = self._create_payload(name="Second", slug="taken-slug")

        refused = self.client.post("/commands/project.create/", payload)
        taken.delete()
        ran = self.client.post("/commands/project.create/", payload)

        self.assertNotEqual(refused.status_code, 302)
        self.assertEqual(ran.status_code, 302)
        self.assertTrue(Project.objects.filter(name="Second").exists())

    def test_unknown_and_repeated_fields_fail_before_execution(self):
        payload = self._create_payload(slug="strict-browser")
        unknown = self.client.post("/commands/project.create/", {**payload, "surprise": "no"})
        repeated = self.client.post(
            "/commands/project.create/",
            {**payload, "name": ["One", "Two"], "__execution_key": "web:repeat"},
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertContains(unknown, "Unknown field: surprise", status_code=400)
        self.assertEqual(repeated.status_code, 400)
        self.assertContains(repeated, "Repeated field: name", status_code=400)
        self.assertFalse(Project.objects.filter(slug="strict-browser").exists())
        self.assertEqual(IdempotencyRecord.objects.count(), 0)

    def test_result_redirect_is_bound_to_an_unguessable_session_token(self):
        response = self.client.post("/commands/project.create/", self._create_payload(slug="result-token"))
        query = parse_qs(urlsplit(response["Location"]).query)

        hidden = self.client.get("/commands/project.create/", {"result": "wrong"})
        shown = self.client.get("/commands/project.create/", {"result": query["result"][0]})

        self.assertIsNone(result_title(hidden))
        self.assertEqual(result_title(shown), "Done")


class CommandResultProjectionTests(TestCase):
    """A result is shown as what it says before it is shown as JSON."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="result-operator",
            email="results@example.test",
            password="strongtestpass-1234",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _run(self, name, payload, form_data):
        key = self.client.get(f"/commands/{name}/").context["form"].initial["__execution_key"]
        with mock.patch("hq.platform.core.command_views.execute_capability", return_value=payload) as execute:
            response = self.client.post(
                f"/commands/{name}/",
                {**form_data, "__execution_key": key},
                follow=True,
            )
        execute.assert_called_once()
        return response

    def test_facts_and_one_flat_list_become_a_grid_and_a_table(self):
        payload = {
            "ok": True,
            "name": "example.test",
            "resolver": "Example",
            "resolves": True,
            "answers": [
                {"name": "example.test", "type": "A", "value": "192.0.2.1"},
                {"name": "example.test", "type": "TXT", "value": "v=spf1 -all", "ttl": None},
            ],
        }

        response = self._run("lookup.name", payload, {"name": "example.test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            dict(response.context["result_facts"]),
            {"name": "example.test", "resolver": "Example", "resolves": "yes"},
        )
        table = response.context["result_table"]
        self.assertEqual(table["label"], "answers")
        self.assertEqual(table["columns"], ("name", "type", "value", "ttl"))
        self.assertEqual(table["rows"][0], ("example.test", "A", "192.0.2.1", MISSING))
        # Each heading is its column's name, then the sort control's mark.
        self.assertIn("type", [heading.split()[0] for heading in texts(response, "th")])
        self.assertIn("answers · 2", text(response))
        self.assertEqual(result_title(response), "Done")
        # The JSON is still there for anyone checking HQ against another tool,
        # closed because the answer is already on the page.
        (raw,) = with_class(response, "command-result-json")
        self.assertNotIn("open", raw.attrs)
        self.assertIn('"resolver": "Example"', raw.words)

    def test_a_shape_the_rules_do_not_fit_shows_the_json_open(self):
        payload = {"ok": True, "nested": {"deep": [1, 2]}, "mixed": [{"a": 1}, {"b": {"c": 2}}]}

        response = self._run("lookup.name", payload, {"name": "example.test"})

        self.assertEqual(response.context["result_facts"], ())
        self.assertIsNone(response.context["result_table"])
        (raw,) = with_class(response, "command-result-json")
        self.assertIn("open", raw.attrs)

    def test_the_projection_is_pure_and_keeps_column_order(self):
        from hq.platform.core.command_views import _result_projection

        facts, table = _result_projection({"ok": True, "count": 2, "note": None, "rows": [{"b": 1, "a": 2}, {"c": 3}]})

        self.assertEqual(facts, (("count", "2"), ("note", MISSING)))
        self.assertEqual(table["columns"], ("b", "a", "c"))
        self.assertEqual(table["rows"], (("1", "2", MISSING), (MISSING, MISSING, "3")))
