from contextlib import nullcontext

from django.db import transaction
from django.test import TestCase
from django.urls import reverse

from hq.platform.core.testing import declare, operator
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow

from ..app_updates import app_updates
from ..finding_steps import Do
from .test_container_standard import runtime
from .test_containers import HIGH, estate, inventory, named, queued
from .test_github_estate import store
from .test_upgrades import TARGET, published

RUNNING = "ghcr.io/example/app:v1.2.0@sha256:aaa"
PINNED = f"ghcr.io/example/app:v1.2.1@{TARGET}"
SERIOUS = {"id": "CVE-new", "package": "libexample", "severity": "critical", "fixed": ["2.0"], "url": "u"}


def moved(before=RUNNING, after=PINNED, service="app"):
    """One service's image as the reading says an update moves it."""

    return {"service": service, "before": before, "after": after}


def pull(number=7, app="app", moves=(), read=True):
    file = {"path": f"compose/{app}/compose.yaml", "status": "modified", "additions": 1, "deletions": 1}
    return {
        "number": number,
        "title": f"Update ghcr.io/example/app Docker tag to v1.2.1 ({number})",
        "url": f"https://github.com/example/alpha/pull/{number}",
        "head": "f" * 40,
        "branch": f"renovate/app-{number}",
        "checks": {"state": "success", "total": 1, "failing": [], "running": 0},
        "files": [file],
        "apps": [{"app": app, "read": read, "images": list(moves)}],
    }


def checked(digest, findings=()):
    return {"digest": f"ghcr.io/example/app@{digest}", "checked": 1, "findings": list(findings), "read_at": "x"}


def arranged(*pulls, scanned=(TARGET,), target_findings=(), advisories=(HIGH,)):
    """A container of example-box's app, its image's reading, and the open ``pulls``."""

    estate(advisories=list(advisories))
    published()
    inventory("portainer.runtime", [runtime("app", service="app")])
    inventory(
        "portainer.compose_project",
        [{"host": "example-box", "name": "app", "config_files": ["/srv/apps/app/compose.yaml"]}],
    )
    inventory(
        "registry.vulnerabilities",
        [checked("sha256:aaa"), *(checked(digest, target_findings) for digest in scanned)],
    )
    store(compose=["compose/app/compose.yaml"], pull_requests=list(pulls))


class AppUpdateTests(TestCase):
    def update(self):
        (found,) = app_updates()
        return found

    def test_a_pin_moved_names_its_app_its_container_and_what_it_fixes(self):
        arranged(pull(moves=[moved()]))

        found = self.update()

        self.assertEqual((found.repository, found.number, found.app, found.head), ("example/alpha", 7, "app", "f" * 40))
        ((service, before, after),) = [(pin.service, pin.before.tag, pin.after.tag) for pin in found.pins]
        self.assertEqual((service, before, after), ("app", "v1.2.0", "v1.2.1"))
        self.assertEqual([item.running.name for item in found.containers], ["app"])
        self.assertEqual([item["id"] for item in found.fixes], ["GHSA-high"])
        self.assertEqual((found.brings, found.scanned, found.readable), ((), True, True))
        self.assertEqual(found.checks_said[0], "Checks passing")

    def test_an_app_whose_files_did_not_load_is_still_the_update_of_its_app(self):
        arranged(pull(read=False))

        found = self.update()

        self.assertEqual((found.readable, found.pins, found.scanned), (False, (), False))
        self.assertEqual(found.url, "https://github.com/example/alpha/pull/7")
        self.assertEqual([item.running.name for item in found.containers], ["app"])

    def test_a_pin_not_yet_scanned_is_never_clean(self):
        arranged(pull(moves=[moved()]), scanned=())

        found = self.update()

        self.assertFalse(found.pins[0].scanned)
        self.assertFalse(found.scanned)
        self.assertNotEqual(found.brings_tone, "good")

    def test_a_digest_only_an_update_pins_is_read_and_scanned(self):
        from ..public_registry import wanted_digests

        held = "sha256:" + "8" * 64
        arranged(pull(moves=[moved(after=f"ghcr.io/example/app:v1.2.1@{held}")]))

        self.assertIn(f"ghcr.io/example/app@{held}", wanted_digests())

    def test_an_app_nothing_runs_moves_no_container(self):
        arranged(pull(app="other", moves=[moved(service="other")]))

        found = self.update()

        self.assertEqual((found.app, found.containers, found.fixes), ("other", (), ()))

    def test_a_serious_finding_the_new_pin_brings_is_named(self):
        arranged(pull(moves=[moved()]), target_findings=[SERIOUS])

        found = self.update()

        self.assertEqual([item["id"] for item in found.brings], ["CVE-new"])
        self.assertEqual(found.brings_tone, "serious")

    def test_the_card_of_the_container_it_moves_names_the_pull_request_and_its_numbers(self):
        arranged(pull(moves=[moved()]))

        (card,) = [item for item in queued() if "app" in named(item, "Container")]

        linked = dict(card.evidence)["Pull request"]
        self.assertEqual((str(linked), linked.url), ("example/alpha#7", "https://github.com/example/alpha/pull/7"))
        self.assertIn(("Pin", "v1.2.0 to v1.2.1"), card.evidence)
        self.assertIn(("Fixes", "1 known vulnerability"), card.evidence)

    def test_the_repositorys_row_of_the_update_names_its_app_and_fix_count(self):
        from ..github_estate import repositories

        arranged(pull(moves=[moved()]))

        (row,) = repositories()["example/alpha"].update_rows

        self.assertIn("app", row.detail)
        self.assertIn("fixes 1", row.detail)

    def test_the_resource_reads_the_same_update_by_its_key(self):
        from ..container_reads import get_app_update, list_app_updates

        arranged(pull(moves=[moved()]))

        (listed,) = list_app_updates()["items"]
        found = get_app_update(listed["key"])

        self.assertEqual((found["app"], found["number"], found["containers"]), ("app", 7, ["example-box:app"]))
        self.assertEqual([item["id"] for item in found["fixes"]], ["GHSA-high"])

    def test_the_cost_does_not_grow_with_the_updates_open(self):
        def arrange(size):
            arranged(*(pull(number, moves=[moved()]) for number in range(1, size + 1)))

        assert_cost_does_not_grow(self, sizes=(1, 8), arrange=arrange, draw=lambda _size: app_updates(), rows=len)


class OneStateTests(TestCase):
    def test_the_finding_and_the_container_page_say_each_state_alike(self):
        from ..container_findings import UPDATE
        from .finding_examples.container_findings import STATES

        operator(self.client)
        spec = {"host": "example-box", "name": "app", "connection_ref": "example-portainer"}
        for index, example in enumerate(STATES):
            with self.subTest(example=index), transaction.atomic():
                declare("example-box-app", "portainer.container", spec)
                with example.given() or nullcontext():
                    (card,) = [item for item in queued(UPDATE) if "app" in named(item, "Container")]
                    page = self.client.get(reverse("control_plane:detail", args=["example-box-app"])).context
                transaction.set_rollback(True)
                act, state = page["container"]["act"], page["container"]["state"]
                self.assertEqual([press.url for press in card.presses], [act.url.partition("?")[0]] if act else [])
                # What holds it back is said once, as its one step.
                told = [step.text for step in card.steps if isinstance(step, Do)]
                self.assertEqual(
                    (card.explanation, told, card.no_help_reason),
                    (state.words, [state.reason] if state.reason else [], ""),
                )
