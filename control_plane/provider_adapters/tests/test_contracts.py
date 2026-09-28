from dataclasses import replace
from unittest import TestCase, mock

from control_plane.provider_adapters import CONTROLLER_PROVIDER_ADAPTERS
from ..contracts import ControllerIntegrationAdapter, compile_controller_adapters


class ControllerProviderAdapterContractTests(TestCase):
    def test_one_contribution_compiles_every_surface(self):
        registry = compile_controller_adapters(
            CONTROLLER_PROVIDER_ADAPTERS, mock.Mock()
        )

        kinds = {"adguard.rewrite", "caddy.route", "github.delivery", "npm.proxy_host"}
        self.assertEqual(set(registry.definitions), kinds)
        self.assertEqual(set(registry.inventory), kinds)
        self.assertEqual(set(registry.connection_probes), {"adguard", "github_app", "npm"})
        self.assertEqual(
            set(registry.actions),
            {
                ("adguard.rewrite", "reconcile"),
                ("adguard.rewrite", "delete"),
                ("caddy.route", "reconcile"),
                ("github.delivery", "reconcile"),
                ("npm.proxy_host", "reconcile"),
                ("npm.proxy_host", "delete"),
            },
        )

    def test_an_adapter_cannot_omit_an_action_its_definition_promises(self):
        with self.assertRaisesRegex(ValueError, "actions do not match"):
            replace(CONTROLLER_PROVIDER_ADAPTERS[0], actions={})

    def test_an_adapter_cannot_omit_a_connection_probe(self):
        adapter = next(
            adapter
            for adapter in CONTROLLER_PROVIDER_ADAPTERS
            if any(
                definition.kind == "adguard.rewrite"
                for definition in adapter.definitions
            )
        )
        with self.assertRaisesRegex(ValueError, "probes do not match"):
            replace(adapter, connection_probes={})

    def test_one_integration_can_emit_multiple_resource_kinds(self):
        first, second = CONTROLLER_PROVIDER_ADAPTERS[:2]
        integration = ControllerIntegrationAdapter(
            definitions=first.definitions + second.definitions,
            inventory={**first.inventory, **second.inventory},
            connection_probes={
                **first.connection_probes,
                **second.connection_probes,
            },
            actions={**first.actions, **second.actions},
        )

        registry = compile_controller_adapters((integration,), mock.Mock())

        self.assertEqual(
            set(registry.definitions),
            {definition.kind for definition in first.definitions + second.definitions},
        )

    def test_admission_rejects_two_adapters_for_one_kind(self):
        adapter = CONTROLLER_PROVIDER_ADAPTERS[0]

        with self.assertRaisesRegex(ValueError, "Duplicate controller adapter"):
            compile_controller_adapters((adapter, adapter), mock.Mock())


class FailureTests(TestCase):
    """Why a read failed, classified where it failed."""

    def test_each_cause_is_classified(self):
        import urllib.error

        from control_plane.provider_adapters.contracts import (
            ADDRESS_FAILURE,
            CREDENTIAL_REFUSAL,
            NETWORK_FAILURE,
            PERMISSION_REFUSAL,
            ProviderError,
            failure_of,
        )

        url = "https://api.example.com"
        cases = (
            (ProviderError("web page", failure=ADDRESS_FAILURE), ADDRESS_FAILURE),
            (ProviderError("refused", refusal=CREDENTIAL_REFUSAL), CREDENTIAL_REFUSAL),
            (urllib.error.HTTPError(url, 401, "", {}, None), CREDENTIAL_REFUSAL),
            (urllib.error.HTTPError(url, 403, "", {}, None), PERMISSION_REFUSAL),
            (urllib.error.HTTPError(url, 502, "", {}, None), ""),
            (urllib.error.URLError("no route"), NETWORK_FAILURE),
            (TimeoutError(), NETWORK_FAILURE),
            (ConnectionRefusedError(), NETWORK_FAILURE),
            (ValueError("bad"), ""),
            (ProviderError("unclassified"), ""),
        )
        for exc, expected in cases:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(failure_of(exc), expected)
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()

    def test_an_unknown_failure_is_refused(self):
        from control_plane.provider_adapters.contracts import ProviderError

        with self.assertRaises(ValueError):
            ProviderError("x", failure="gremlins")
