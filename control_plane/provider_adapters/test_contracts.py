from dataclasses import replace
from unittest import TestCase, mock

from control_plane.providers import CONTROLLER_PROVIDER_ADAPTERS
from .contracts import compile_controller_adapters


class ControllerProviderAdapterContractTests(TestCase):
    def test_one_contribution_compiles_every_surface(self):
        registry = compile_controller_adapters(
            CONTROLLER_PROVIDER_ADAPTERS, mock.Mock()
        )

        self.assertEqual(
            set(registry.definitions), {"adguard.rewrite", "caddy.route"}
        )
        self.assertEqual(
            set(registry.inventory), {"adguard.rewrite", "caddy.route"}
        )
        self.assertEqual(set(registry.connection_probes), {"adguard"})
        self.assertEqual(
            set(registry.actions),
            {
                ("adguard.rewrite", "reconcile"),
                ("adguard.rewrite", "delete"),
                ("caddy.route", "reconcile"),
            },
        )

    def test_an_adapter_cannot_omit_an_action_its_definition_promises(self):
        with self.assertRaisesRegex(ValueError, "actions do not match"):
            replace(CONTROLLER_PROVIDER_ADAPTERS[0], actions={})

    def test_an_adapter_cannot_omit_a_connection_probe(self):
        adapter = next(
            adapter
            for adapter in CONTROLLER_PROVIDER_ADAPTERS
            if adapter.definition.kind == "adguard.rewrite"
        )
        with self.assertRaisesRegex(ValueError, "probes do not match"):
            replace(adapter, connection_probes={})

    def test_admission_rejects_two_adapters_for_one_kind(self):
        adapter = CONTROLLER_PROVIDER_ADAPTERS[0]

        with self.assertRaisesRegex(ValueError, "Duplicate controller adapter"):
            compile_controller_adapters((adapter, adapter), mock.Mock())
