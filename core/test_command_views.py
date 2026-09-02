"""Browser execution tests for the registry-driven Command Center."""

import json
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase

from application.capabilities import capability_registry
from application.command_targets import capability_target_options
from application.security import Capability, Principal
from control_plane.models import ManagedResource
from core.models import AuditLog
from hq_api.models import IdempotencyRecord
from projects.models import Project


User = get_user_model()


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
            mock.patch("core.command_views.web_principal", return_value=principal),
            mock.patch("core.command_views.command_form_class") as form_factory,
        ):
            response = self.client.get("/commands/project.create/")

        self.assertEqual(response.status_code, 403)
        form_factory.assert_not_called()

    def test_form_and_machine_contract_come_from_the_registered_schema(self):
        response = self.client.get("/commands/project.create/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="name"')
        self.assertContains(response, 'name="slug"')
        self.assertContains(response, 'name="__execution_key"')
        self.assertIn('"additionalProperties": false', response.context["schema_json"])
        self.assertNotContains(response, 'name="__target"')

    def test_targeted_command_derives_its_target_control(self):
        Project.objects.create(name="HQ", slug="hq")
        response = self.client.get("/commands/project.update/", {"target": "hq"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="__target"')
        self.assertContains(response, 'value="hq"')
        self.assertContains(response, 'name="__expected_updated_at"')

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

        self.assertContains(response, "Certificate key")
        self.assertContains(response, "The managed certificate to renew.")
        self.assertContains(response, '<option value="example-certificate">')
        self.assertNotContains(response, '<option value="example-zone">')
        self.assertContains(response, 'type="hidden" name="idempotency_key"')
        self.assertNotContains(response, ">Idempotency Key<")
        self.assertContains(
            response, "1 eligible target from HQ's authorized local catalog"
        )
        self.assertContains(response, "certificate.renew → request_certificate_renewal")
        self.assertContains(response, "provider work runs outside this page request")

    def test_target_choices_are_one_local_query_and_never_a_provider_lookup(self):
        ManagedResource.objects.create(
            key="example-certificate",
            kind="tls.certificate",
            spec={},
        )
        principal = Principal(
            "operator",
            "web",
            frozenset({Capability.READ, Capability.REQUEST_CERTIFICATE_RENEWAL}),
        )

        with self.assertNumQueries(1):
            options = capability_target_options(
                capability_registry()["certificate.renew"], principal=principal
            )

        self.assertEqual(
            [option.value for option in options or ()], ["example-certificate"]
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

        response = self.client.get(
            "/commands/infrastructure.reconcile/", {"kind": "tailscale.device"}
        )

        self.assertContains(
            response,
            '<option value="example-device">example-device · Tailscale Device</option>',
            html=True,
        )
        self.assertNotContains(response, '<option value="example-zone">')
        self.assertContains(
            response, "1 eligible target from HQ's authorized local catalog"
        )

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
        self.assertEqual(
            form["__expected_updated_at"].value(), resource.updated_at.isoformat()
        )
        self.assertContains(response, "data-command-hydrate-target")

    def test_success_uses_prg_and_the_application_audit_boundary(self):
        response = self.client.post(
            "/commands/project.create/", self._create_payload(), follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Committed once")
        self.assertTrue(Project.objects.filter(slug="browser-command").exists())
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
        audit = AuditLog.objects.get(object_type="Project")
        self.assertEqual(audit.metadata["interface"], "web")
        self.assertEqual(audit.metadata["operation"], "project.create")

    def test_a_contextual_command_returns_to_the_workflow_that_offered_it(self):
        source = "/infrastructure/findings/"
        form = self.client.get(
            "/commands/project.create/", {"next": source}
        ).context["form"]
        payload = {
            "name": "Contextual command",
            "slug": "contextual-command",
            "__execution_key": form.initial["__execution_key"],
            "next": form.initial["next"],
        }

        response = self.client.post(
            "/commands/project.create/", payload, follow=True
        )

        self.assertContains(response, f'href="{source}"')
        self.assertContains(response, "Return to previous workflow")

    def test_a_command_never_returns_to_an_external_site(self):
        response = self.client.get(
            "/commands/project.create/", {"next": "https://attacker.example/"}
        )

        self.assertEqual(response.context["form"].initial["next"], "")
        self.assertEqual(response.context["return_url"], "")

    def test_exact_retry_replays_one_committed_write(self):
        payload = self._create_payload(slug="safe-retry")

        first = self.client.post("/commands/project.create/", payload)
        second = self.client.post("/commands/project.create/", payload, follow=True)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "Safely replayed")
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
        self.assertContains(
            response, "already used for a different request", status_code=409
        )
        self.assertFalse(Project.objects.filter(slug="changed-request").exists())

    def test_unknown_and_repeated_fields_fail_before_execution(self):
        payload = self._create_payload(slug="strict-browser")
        unknown = self.client.post(
            "/commands/project.create/", {**payload, "surprise": "no"}
        )
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
        response = self.client.post(
            "/commands/project.create/", self._create_payload(slug="result-token")
        )
        query = parse_qs(urlsplit(response["Location"]).query)

        hidden = self.client.get("/commands/project.create/", {"result": "wrong"})
        shown = self.client.get(
            "/commands/project.create/", {"result": query["result"][0]}
        )

        self.assertNotContains(hidden, "Committed once")
        self.assertContains(shown, "Committed once")


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
        key = self.client.get(f"/commands/{name}/").context["form"].initial[
            "__execution_key"
        ]
        with mock.patch(
            "core.command_views.execute_capability", return_value=payload
        ) as execute:
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
        self.assertEqual(table["rows"][0], ("example.test", "A", "192.0.2.1", "—"))
        self.assertContains(response, "<th>type</th>")
        self.assertContains(response, "answers · 2")
        self.assertContains(response, "Ran once")
        # The JSON is still there for anyone checking HQ against another tool,
        # closed because the answer is already on the page.
        self.assertContains(response, '<details class="command-result-json">')
        self.assertContains(response, "&quot;resolver&quot;: &quot;Example&quot;")

    def test_a_shape_the_rules_do_not_fit_shows_the_json_open(self):
        payload = {"ok": True, "nested": {"deep": [1, 2]}, "mixed": [{"a": 1}, {"b": {"c": 2}}]}

        response = self._run("lookup.name", payload, {"name": "example.test"})

        self.assertEqual(response.context["result_facts"], ())
        self.assertIsNone(response.context["result_table"])
        self.assertContains(response, '<details class="command-result-json" open>')

    def test_the_projection_is_pure_and_keeps_column_order(self):
        from core.command_views import _result_projection

        facts, table = _result_projection(
            {"ok": True, "count": 2, "note": None, "rows": [{"b": 1, "a": 2}, {"c": 3}]}
        )

        self.assertEqual(facts, (("count", "2"), ("note", "—")))
        self.assertEqual(table["columns"], ("b", "a", "c"))
        self.assertEqual(table["rows"], (("1", "2", "—"), ("—", "—", "3")))
