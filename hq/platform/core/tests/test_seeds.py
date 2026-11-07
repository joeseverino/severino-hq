"""Every tool takes its data by one name from one registry, which reaches no test code."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.config import sandbox
from hq.domains.projects.models import Project
from hq.platform.application import plugin_testing
from hq.platform.application.security import WEB_INTERFACE
from hq.platform.core import seeds
from hq.platform.core.audit import Unattributed
from hq.platform.core.example_estate import example_owner
from hq.platform.core.example_estate.names import OWNER_USERNAME
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.models import AuditLog
from hq.platform.core.tests import test_browser_fixtures
from hq.platform.core.tests.browser import estate


class RegistryTests(SimpleTestCase):
    def test_an_unknown_name_is_refused_with_every_known_one(self):
        with self.assertRaisesMessage(LookupError, ", ".join(seeds.SEEDS)):
            seeds.get("no-such-set")

    def test_a_name_is_registered_once(self):
        with self.assertRaisesMessage(ValueError, "showcase"):
            seeds.register("showcase", seeds.SEEDS["empty"])

        self.assertIsNot(seeds.get("showcase"), seeds.SEEDS["empty"])

    def test_a_stress_estate_is_known_by_name_once_its_builder_is_loaded(self):
        # Read first: the module registers its estates as it loads, which a lazy import waits for.
        builder = test_browser_fixtures.build_estate
        self.assertIs(seeds.get("sparse").plant, builder)

    def test_the_server_side_of_the_registry_loads_no_test_module(self):
        probe = (
            "import django, json, sys; django.setup()\n"
            "from hq.platform.core.management.commands import seed\n"
            "print(json.dumps([m for m in sys.modules if 'tests' in m.split('.')]))\n"
        )
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "hq.config.settings", "DJANGO_SECRET_KEY": "x" * 50}
        loaded = subprocess.run(
            [sys.executable, "-c", probe], cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, check=True
        )

        self.assertEqual(json.loads(loaded.stdout.splitlines()[-1]), [])


class BuiltTests(TestCase):
    def test_a_set_is_built_under_the_settings_it_is_shown_under(self):
        with throwaway_media(), seeds.built("showcase") as owner:
            self.assertEqual(settings.SEVERINO_POSTURE, "advanced")
            self.assertEqual(owner.username, OWNER_USERNAME)

        self.assertEqual(settings.SEVERINO_POSTURE, "")


def _with_demo(written: list, pages=(), seed=None) -> tuple:
    """A synthetic extension whose demo records who it was written for, or runs ``seed``."""

    manifest, integration = plugin_testing.sibling(identifier="example.demo", name="Demo")
    return manifest, replace(integration, demo_seed=seed or written.append, demo_pages=lambda: pages)


def _project() -> Project:
    return Project.objects.create(name="Example demo", slug="example-demo")


class DemoTests(TestCase):
    def test_the_showcase_and_standard_sets_write_each_extensions_demo_for_their_owner(self):
        for name in ("showcase", "standard"):
            with self.subTest(name), transaction.atomic():
                written: list = []
                with plugin_testing.installed(_with_demo(written)), throwaway_media(), seeds.built(name) as owner:
                    self.assertEqual(written, [owner])
                transaction.set_rollback(True)

    def test_a_set_of_its_own_writes_no_extensions_demo(self):
        written: list = []
        with plugin_testing.installed(_with_demo(written)), seeds.built("empty"):
            self.assertEqual(written, [])

    def test_the_estates_record_pages_hold_each_page_an_extensions_demo_names(self):
        with plugin_testing.installed(_with_demo([], pages=("/examples/demo/7/",))):
            self.assertIn("/examples/demo/7/", estate.records())
            self.assertIn("/examples/demo/7/", estate.estate_pages())

    def test_a_record_a_demo_made_by_hand_is_the_owners_in_the_browser_when_it_says(self):
        made = timezone.now() - timedelta(days=35)

        def seed(_owner):
            with seeds.seeded_by_hand(reverse("projects:create"), at=made):
                _project()

        with plugin_testing.installed(_with_demo([], seed=seed)), throwaway_media(), seeds.built("standard") as owner:
            (event,) = AuditLog.objects.filter(
                object_type="Project", object_id=str(Project.objects.get(slug="example-demo").pk)
            )

        self.assertEqual(
            (event.user, event.interface, event.metadata["operation"], event.created_at),
            (owner, WEB_INTERFACE, "projects:create", made),
        )

    def test_a_record_a_demo_writes_without_saying_how_it_was_made_is_refused(self):
        with (
            plugin_testing.installed(_with_demo([], seed=lambda _owner: _project())),
            throwaway_media(),
            self.assertRaises(Unattributed),
            seeds.built("standard"),
        ):
            pass

    def test_nothing_but_a_demo_being_written_records_events_made_by_hand(self):
        with (
            self.assertRaisesMessage(seeds.SeedRefused, "Only an extension's demo"),
            seeds.seeded_by_hand(reverse("projects:create"), at=timezone.now()),
        ):
            _project()

        self.assertFalse(Project.objects.exists())

    def test_a_deployments_process_writes_no_demo_even_into_a_new_database(self):
        written: list = []
        outside_a_run = {name: value for name, value in os.environ.items() if name != sandbox.RUN}
        with (
            mock.patch.dict(os.environ, outside_a_run, clear=True),
            plugin_testing.installed(_with_demo(written)),
            override_settings(DEBUG=False),
            self.assertRaisesMessage(seeds.SeedRefused, "development server or a test run"),
        ):
            seeds.plant("showcase", example_owner())

        self.assertEqual(written, [])
        self.assertEqual(seeds.records_held(), {})
