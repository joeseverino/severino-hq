"""What the palette offers beside the registries' resources: the checks HQ can
make, and its commands in the order an operator looks for them."""

from django.test import SimpleTestCase

from ..command_center import command_center
from ..findings import finding_rules
from ..integrations import integration_graph
from ..security import Capability, Principal
from .host_only import host_only

READ = Principal("reader", "test", frozenset({Capability.READ}))
OPERATOR = Principal("operator", "test", frozenset(Capability))


class CheckDiscoveryTests(SimpleTestCase):
    def test_a_reader_is_offered_every_check_linked_to_its_findings(self):
        with host_only():
            checks = command_center("", principal=READ)["checks"]

        self.assertEqual([item.name for item in checks], [rule.name for rule in finding_rules()])
        for item in checks:
            self.assertIn(f"rule={item.name}", item.url)

    def test_a_query_narrows_the_checks_to_the_rule_it_names(self):
        rule = finding_rules()[0]
        with host_only():
            checks = command_center(rule.title, principal=READ)["checks"]

        self.assertEqual(checks[0].name, rule.name)


class CommandOrderTests(SimpleTestCase):
    def test_without_a_query_the_commands_keep_the_registry_order(self):
        with host_only():
            names = [item.name for item in command_center("", principal=OPERATOR)["commands"]]
            registered = [spec.name for spec in integration_graph().capabilities.values()]

        self.assertGreater(len(names), 1)
        self.assertEqual(names, [name for name in registered if name in names])

    def test_a_query_puts_the_command_it_names_first(self):
        with host_only():
            commands = command_center("certificate renew", principal=OPERATOR)["commands"]

        self.assertEqual(commands[0].name, "certificate.renew")
        self.assertGreater(len(commands), 1)
