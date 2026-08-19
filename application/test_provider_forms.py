"""The form is generated from the provider model, and validated by it.

Two properties matter and both are easy to lose. A provider added to the
registry must get a working create-and-edit page with nothing written for it;
and the form must never become a second opinion about what is valid.

Hostnames here are under example.com. This repository is public, and a fixture
is documentation whether or not it was meant to be.
"""

from __future__ import annotations

from dataclasses import replace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ManagedResource
from control_plane.providers import PROVIDERS

from .provider_forms import spec_form_class

REWRITE = {"domain": "app.example.com", "answer": "10.0.0.10"}
PROXY = {
    "domain_names": "app.example.com\nwww.example.com",
    "forward_scheme": "http",
    "forward_host": "10.0.0.10",
    "forward_port": "8000",
}


class GeneratedFieldTests(TestCase):
    """Each pydantic annotation becomes the input that collects it."""

    def test_a_literal_becomes_a_choice_the_operator_cannot_get_wrong(self):
        form = spec_form_class("npm.proxy_host")()

        self.assertEqual(
            [value for value, _ in form.fields["forward_scheme"].choices],
            ["http", "https"],
        )

    def test_bounds_come_from_the_model(self):
        field = spec_form_class("npm.proxy_host")().fields["forward_port"]

        self.assertEqual((field.min_value, field.max_value), (1, 65535))

    def test_a_list_field_accepts_one_name_per_line(self):
        form = spec_form_class("npm.proxy_host")(PROXY)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.spec["domain_names"], ["app.example.com", "www.example.com"]
        )

    def test_a_checkbox_is_never_required(self):
        """required=True on a BooleanField means "must be ticked", which is wrong.

        Every optional flag would have to be turned on before the form would
        submit -- including the ones whose model default is False.
        """
        self.assertFalse(spec_form_class("npm.proxy_host")().fields["websocket"].required)

    def test_an_omitted_optional_falls_back_to_the_model_default(self):
        """Sent as None it would be rejected; restated here it could drift."""
        form = spec_form_class("cloudflare.dns_record")(
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.10",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.spec["ttl"], 1)


class ValidationOwnershipTests(TestCase):
    """The model decides what is valid. The form only collects it."""

    def test_a_pattern_on_the_model_is_enforced_by_the_form(self):
        form = spec_form_class("tls.certificate")({"topology_ref": "not-a-reference"})

        self.assertFalse(form.is_valid())
        self.assertIn("topology_ref", form.errors)

    def test_a_model_rule_no_widget_could_express_is_still_enforced(self):
        """`extra="forbid"` and cross-field rules live only in pydantic.

        A form that validated on its own would accept this and hand the API a
        payload the same code rejects, which is the drift these tests exist to
        prevent.
        """
        form = spec_form_class("npm.proxy_host")({**PROXY, "forward_port": "70000"})

        self.assertFalse(form.is_valid())
        self.assertIn("forward_port", form.errors)

    def test_a_pydantic_failure_is_reported_against_its_own_field(self):
        form = spec_form_class("adguard.rewrite")({"domain": "", "answer": ""})

        self.assertFalse(form.is_valid())
        self.assertIn("domain", form.errors)


class ProviderOnboardingTests(TestCase):
    """A provider added to the registry gets a form with nothing written."""

    def test_an_invented_provider_renders_and_validates_without_code(self):
        invented = replace(
            PROVIDERS["adguard.rewrite"],
            kind="invented.rewrite",
            summary="A provider that did not exist when the form engine was written.",
        )
        PROVIDERS[invented.kind] = invented
        try:
            form = spec_form_class(invented.kind)(REWRITE)
            valid = form.is_valid()
            fields = sorted(form.fields)
        finally:
            del PROVIDERS[invented.kind]

        self.assertTrue(valid)
        self.assertEqual(fields, ["answer", "domain"])


class IdentityFieldTests(TestCase):
    """A provider finds its own record by hostname, so a hostname cannot move.

    AdGuard matches the rewrite whose ``domain`` equals the spec's; NPM matches
    the host whose ``domain_names`` match. Change one and reconciliation looks
    for the new name, does not find it, and creates it -- leaving the old record
    in place and serving. Neither has a delete path, so nothing cleans that up.
    """

    def test_identity_is_read_from_what_a_hostname_seeds(self):
        from .provider_forms import identity_fields

        self.assertEqual(identity_fields("adguard.rewrite"), ("domain",))
        self.assertEqual(identity_fields("npm.proxy_host"), ("domain_names",))
        # A certificate is not addressed by hostname, so it locks nothing.
        self.assertEqual(identity_fields("tls.certificate"), ())

    def test_creating_leaves_the_hostname_editable(self):
        self.assertFalse(spec_form_class("adguard.rewrite")().fields["domain"].disabled)

    def test_editing_holds_the_hostname_against_a_crafted_post(self):
        """Disabled fields ignore submitted data, which is the point.

        A validation error would be enough for a stray click. This has to hold
        against a hand-made request too, because the damage is silent, happens
        at the provider, and cannot be undone from HQ.
        """
        form = spec_form_class("adguard.rewrite", lock_identity=True)(
            {"domain": "attacker.example.com", "answer": "10.0.0.11"},
            initial=REWRITE,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.spec["domain"], "app.example.com")
        self.assertEqual(form.spec["answer"], "10.0.0.11")

    def test_the_edit_page_locks_it_and_says_why(self):
        user = get_user_model().objects.create_user(
            username="operator", password="test-only-password"
        )
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="app-dns", kind="adguard.rewrite", spec=REWRITE
        )

        response = self.client.get(
            reverse("control_plane:edit", kwargs={"key": "app-dns"})
        )

        self.assertContains(response, "disabled")
        self.assertContains(response, "would create a second record")


class ResourceFormViewTests(TestCase):
    """The web write goes through the same use case the API and MCP call."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="test-only-password"
        )
        self.client.force_login(self.user)

    def test_declaring_a_resource_writes_desired_state(self):
        response = self.client.post(
            reverse("control_plane:create"),
            {"kind": "adguard.rewrite", "key": "app-dns", "enabled": "on", **REWRITE},
        )

        resource = ManagedResource.objects.get(key="app-dns")
        self.assertRedirects(
            response, reverse("control_plane:detail", kwargs={"key": "app-dns"})
        )
        self.assertEqual(resource.spec, REWRITE)
        self.assertTrue(resource.enabled)

    def test_a_new_declaration_is_fingerprinted(self):
        """Otherwise the next topology import reads the blank as a change."""
        self.client.post(
            reverse("control_plane:create"),
            {"kind": "adguard.rewrite", "key": "app-dns", "enabled": "on", **REWRITE},
        )

        self.assertTrue(ManagedResource.objects.get(key="app-dns").desired_fingerprint)

    def test_editing_advances_the_generation_so_the_controller_reapplies(self):
        self.client.post(
            reverse("control_plane:create"),
            {"kind": "adguard.rewrite", "key": "app-dns", "enabled": "on", **REWRITE},
        )
        before = ManagedResource.objects.get(key="app-dns").generation

        self.client.post(
            reverse("control_plane:edit", kwargs={"key": "app-dns"}),
            {"key": "app-dns", "enabled": "on", **REWRITE, "answer": "10.0.0.11"},
        )

        resource = ManagedResource.objects.get(key="app-dns")
        self.assertEqual(resource.spec["answer"], "10.0.0.11")
        self.assertEqual(resource.generation, before + 1)

    def test_an_invalid_spec_writes_nothing_and_says_why(self):
        response = self.client.post(
            reverse("control_plane:create"),
            {
                "kind": "adguard.rewrite",
                "key": "app-dns",
                "enabled": "on",
                "domain": "app.example.com",
                "answer": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertFalse(ManagedResource.objects.exists())

    def test_a_duplicate_key_is_reported_rather_than_raised(self):
        ManagedResource.objects.create(
            key="app-dns", kind="adguard.rewrite", spec=REWRITE
        )

        response = self.client.post(
            reverse("control_plane:create"),
            {"kind": "adguard.rewrite", "key": "app-dns", "enabled": "on", **REWRITE},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ManagedResource.objects.filter(key="app-dns").count(), 1)

    def test_choosing_a_kind_lists_every_provider_from_the_registry(self):
        response = self.client.get(reverse("control_plane:create"))

        self.assertEqual(response.status_code, 200)
        for kind in PROVIDERS:
            self.assertContains(response, kind)

    def test_a_hostname_seeds_the_fields_it_decides(self):
        """Onboarding asks for the name once, not once per resource."""
        response = self.client.get(
            reverse("control_plane:create"),
            {"kind": "adguard.rewrite", "hostname": "app.example.com"},
        )

        self.assertContains(response, "app.example.com")
        # And suggests a key, so nothing stops to invent a name for a row the
        # operator did not know existed.
        self.assertContains(response, "app-example-com-dns")

    def test_the_form_requires_a_signed_in_operator(self):
        self.client.logout()

        response = self.client.get(reverse("control_plane:create"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
