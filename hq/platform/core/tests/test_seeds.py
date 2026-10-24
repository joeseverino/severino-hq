"""Every tool takes its data by one name from one registry, and the shipped
registry reaches no test code."""

import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from hq.platform.core import seeds
from hq.platform.core.example_estate import throwaway_media
from hq.platform.core.tests import test_browser_fixtures


class RegistryTests(SimpleTestCase):
    def test_an_unknown_name_is_refused_with_every_known_one(self):
        with self.assertRaises(LookupError) as refused:
            seeds.get("no-such-set")

        for name in seeds.SEEDS:
            self.assertIn(name, str(refused.exception))

    def test_a_name_is_registered_once(self):
        with self.assertRaisesMessage(ValueError, "showcase"):
            seeds.register("showcase", seeds.Seed(seeds.SEEDS["empty"].plant))

        self.assertIs(seeds.get("showcase").plant, seeds.SEEDS["showcase"].plant)

    def test_a_stress_estate_is_known_by_name_once_its_builder_is_loaded(self):
        self.assertIs(seeds.get("sparse").plant, test_browser_fixtures.build_estate)

    def test_importing_the_registry_and_its_command_loads_no_test_module(self):
        """The server ships the registry; the stress estates register from the
        test tree, so the shipped half must never import it."""

        probe = (
            "import django, json, sys; django.setup()\n"
            "from hq.platform.core import seeds\n"
            "from hq.platform.core.management.commands import seed\n"
            "for name in list(seeds.SEEDS): seeds.get(name).plant\n"
            "print(json.dumps([m for m in sys.modules if 'tests' in m.split('.')]))\n"
        )
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "hq.config.settings", "DJANGO_SECRET_KEY": "x" * 50}
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, check=True
        )

        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), [])


class BuiltTests(TestCase):
    def test_a_set_is_built_under_the_settings_it_is_shown_under(self):
        with throwaway_media(), seeds.built("showcase") as owner:
            self.assertEqual(settings.SEVERINO_POSTURE, "advanced")
            self.assertEqual(owner.username, "example")

        self.assertEqual(settings.SEVERINO_POSTURE, "")
