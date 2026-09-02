from dataclasses import replace
from unittest import TestCase, mock

from control_plane.providers import CONTROLLER_PROVIDER_ADAPTERS
from .contracts import ControllerIntegrationAdapter, compile_controller_adapters


class ControllerProviderAdapterContractTests(TestCase):
    def test_one_contribution_compiles_every_surface(self):
        registry = compile_controller_adapters(
            CONTROLLER_PROVIDER_ADAPTERS, mock.Mock()
        )

        self.assertEqual(
            set(registry.definitions),
            {"adguard.rewrite", "caddy.route", "npm.proxy_host"},
        )
        self.assertEqual(
            set(registry.inventory),
            {"adguard.rewrite", "caddy.route", "npm.proxy_host"},
        )
        self.assertEqual(set(registry.connection_probes), {"adguard", "npm"})
        self.assertEqual(
            set(registry.actions),
            {
                ("adguard.rewrite", "reconcile"),
                ("adguard.rewrite", "delete"),
                ("caddy.route", "reconcile"),
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
