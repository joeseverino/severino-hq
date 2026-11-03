"""A unit is named for a person by its unit file's title, with its name kept as
the identifier systemctl takes, wherever HQ shows one."""

import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from ..findings import derive_findings
from ..projection import projection_scope
from ..topology import derive_topology
from .test_tailnet_posture import EVERYTHING
from .test_unit_findings import SERVICE, SPEC, failed, service, sweep, timer

ROOT = Path(__file__).resolve().parents[4]
MOUNT = r"run-example\x2dsecrets.mount"
MOUNT_TITLE = "Example credential tmpfs, kept out of swap"
TIMER = "severino-hq-example.timer"
TIMER_TITLE = "Run the example job nightly"
SERVICE_TITLE = "Do the example job"


def mounted(**said) -> dict:
    return service(MOUNT, file_state="enabled", active="active", sub="mounted", title=MOUNT_TITLE, **said)


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("operator"))

    def test_the_hq_page_names_each_unit_by_its_title_and_keeps_its_name(self):
        sweep(mounted(), timer(TIMER, title=TIMER_TITLE), service(title=SERVICE_TITLE))

        page = self.client.get(reverse("hq_self")).content.decode()

        for title in (MOUNT_TITLE, TIMER_TITLE, SERVICE_TITLE):
            self.assertIn(title, page)
        # The name stays, as the identifier systemctl takes.
        self.assertIn("run-example", page)

    def test_a_unit_with_no_title_is_named_by_its_name(self):
        sweep(service())

        page = self.client.get(reverse("hq_self"))

        self.assertContains(page, "severino-hq-example.service")


class FindingTests(TestCase):
    def test_a_finding_names_the_unit_by_its_title_and_runs_systemctl_on_its_name(self):
        sweep(failed(title=SERVICE_TITLE))

        with projection_scope():
            (finding,) = [
                found
                for found in derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
                if found.rule == "unit-failed"
            ]

        self.assertIn(SERVICE_TITLE, finding.title)
        self.assertNotIn(SERVICE, finding.title)
        self.assertTrue(all(SERVICE in step.command for step in finding.steps), finding.steps)


class RecordTests(SimpleTestCase):
    def test_a_title_that_is_a_path_or_an_assignment_is_refused(self):
        for title in ("/run/example/token", "EXAMPLE_TOKEN=sentinel-value", "a | b", "a" * 121):
            with self.subTest(title=title):
                self.assertEqual(SPEC.clean([service(title=title)]), ([], 1))


@tag("source")
class SeedTests(SimpleTestCase):
    def test_the_seed_titles_each_unit_as_its_shipped_file_does(self):
        from hq.platform.core.example_estate.work import TITLES

        shipped = {}
        for path in (ROOT / "deploy" / "systemd").iterdir():
            if path.is_file() and (said := re.search(r"(?m)^Description=(.*)$", path.read_text())):
                shipped[path.name] = said.group(1)

        for unit, title in TITLES.items():
            with self.subTest(unit=unit):
                instance = unit.partition("@")[2].rpartition(".")[0]
                template = re.sub(r"@[^.]+\.", "@.", unit)
                self.assertEqual(title, shipped[template].replace("%i", instance) if instance else shipped[unit])
                self.assertEqual(SPEC.clean([service(unit, title=title)])[1], 0)
