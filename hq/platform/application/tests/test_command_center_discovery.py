"""What the palette offers beside the registries' resources: the checks HQ can
make, and its commands in the order an operator looks for them."""

from django.test import SimpleTestCase

from hq.platform.core.testing import OPERATOR, READER

from ..capabilities import capability_registry
from ..command_center import command_center
from ..findings import finding_rules
from .host_only import host_only


class CheckDiscoveryTests(SimpleTestCase):
    def test_a_query_narrows_the_checks_to_the_rule_it_names(self):
        rule = finding_rules()[0]
        with host_only():
            checks = command_center(rule.title, principal=READER)["checks"]

        self.assertEqual(checks[0].name, rule.name)


class CommandOrderTests(SimpleTestCase):
    def test_without_a_query_the_commands_keep_the_registry_order(self):
        with host_only():
            names = [item.name for item in command_center("", principal=OPERATOR)["commands"]]
            registered = [spec.name for spec in capability_registry()]

        self.assertGreater(len(names), 1)
        self.assertEqual(names, [name for name in registered if name in names])

    def test_a_query_puts_the_commands_it_names_first(self):
        with host_only():
            commands = command_center("record", principal=OPERATOR)["commands"]

        named = ["record" in item.label.casefold() for item in commands]
        self.assertIn(False, named)
        self.assertEqual(named, sorted(named, reverse=True))
