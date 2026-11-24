from django.test import TestCase

from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow

from ..app_updates import app_updates
from .test_container_standard import runtime
from .test_containers import HIGH, estate, inventory, queued
from .test_github_estate import store
from .test_upgrades import TARGET, published

RUNNING = "ghcr.io/example/app:v1.2.0@sha256:aaa"
PINNED = f"ghcr.io/example/app:v1.2.1@{TARGET}"
SERIOUS = {"id": "CVE-new", "package": "libexample", "severity": "critical", "fixed": ["2.0"], "url": "u"}


def patch(before=RUNNING, after=PINNED, service="app"):
    return (
        "@@ -1,5 +1,5 @@\n services:\n"
        f'   {service}:\n-    image: {before}\n+    image: "{after}"  # pinned\n     restart: unless-stopped'
    )


def pull(number=7, app="app", diff=None):
    file = {"path": f"compose/{app}/compose.yaml", "status": "modified", "additions": 1, "deletions": 1}
    return {
        "number": number,
        "title": f"Update ghcr.io/example/app Docker tag to v1.2.1 ({number})",
        "url": f"https://github.com/example/alpha/pull/{number}",
        "head": "f" * 40,
        "branch": f"renovate/app-{number}",
        "checks": {"state": "success", "total": 1, "failing": [], "running": 0},
        "files": [file | ({"patch": diff} if diff else {})],
    }


def checked(digest, findings=()):
    return {"digest": f"ghcr.io/example/app@{digest}", "checked": 1, "findings": list(findings), "read_at": "x"}


def arranged(*pulls, scanned=(TARGET,), target_findings=()):
    """A container of example-box's app, its image's reading, and the open ``pulls``."""

    estate(advisories=[HIGH])
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
    store(pull_requests=list(pulls))


class AppUpdateTests(TestCase):
    def update(self):
        (found,) = app_updates()
        return found

    def test_a_pin_moved_names_its_app_its_container_and_what_it_fixes(self):
        arranged(pull(diff=patch()))

        found = self.update()

        self.assertEqual((found.repository, found.number, found.app, found.head), ("example/alpha", 7, "app", "f" * 40))
        ((service, before, after),) = [(pin.service, pin.before.tag, pin.after.tag) for pin in found.pins]
        self.assertEqual((service, before, after), ("app", "v1.2.0", "v1.2.1"))
        self.assertEqual([item.running.name for item in found.containers], ["app"])
        self.assertEqual([item["id"] for item in found.fixes], ["GHSA-high"])
        self.assertEqual((found.brings, found.scanned, found.readable), ((), True, True))
        self.assertEqual(found.checks_said[0], "Checks passing")

    def test_a_diff_too_large_to_read_is_still_the_update_of_its_app(self):
        arranged(pull())

        found = self.update()

        self.assertEqual((found.readable, found.pins, found.scanned), (False, (), False))
        self.assertEqual(found.url, "https://github.com/example/alpha/pull/7")
        self.assertEqual([item.running.name for item in found.containers], ["app"])

    def test_a_pin_not_yet_scanned_is_never_clean(self):
        arranged(pull(diff=patch()), scanned=())

        found = self.update()

        self.assertFalse(found.pins[0].scanned)
        self.assertFalse(found.scanned)
        self.assertNotEqual(found.brings_tone, "good")

    def test_a_digest_only_an_update_pins_is_read_and_scanned(self):
        from ..public_registry import wanted_digests

        held = "sha256:" + "8" * 64
        arranged(pull(diff=patch(after=f"ghcr.io/example/app:v1.2.1@{held}")))

        self.assertIn(f"ghcr.io/example/app@{held}", wanted_digests())

    def test_an_app_nothing_runs_moves_no_container(self):
        arranged(pull(app="other", diff=patch(service="other")))

        found = self.update()

        self.assertEqual((found.app, found.containers, found.fixes), ("other", (), ()))

    def test_a_serious_finding_the_new_pin_brings_is_named(self):
        arranged(pull(diff=patch()), target_findings=[SERIOUS])

        found = self.update()

        self.assertEqual([item["id"] for item in found.brings], ["CVE-new"])
        self.assertEqual(found.brings_tone, "serious")

    def test_the_card_of_the_container_it_moves_names_the_pull_request_and_its_numbers(self):
        arranged(pull(diff=patch()))

        (card,) = [item for item in queued() if ("Container", "app") in item.evidence]

        self.assertIn(("Pull request", "example/alpha#7"), card.evidence)
        self.assertIn(("Pin", "v1.2.0 to v1.2.1"), card.evidence)
        self.assertIn(("Fixes", "1 known vulnerability"), card.evidence)
        self.assertEqual(card.primary.label, "Review #7 (fixes 1)")

    def test_the_repositorys_row_of_the_update_names_its_app_and_fix_count(self):
        from ..github_estate import repositories

        arranged(pull(diff=patch()))

        (row,) = repositories()["example/alpha"].update_rows

        self.assertIn("app", row.detail)
        self.assertIn("fixes 1", row.detail)

    def test_the_resource_reads_the_same_update_by_its_key(self):
        from ..container_reads import get_app_update, list_app_updates

        arranged(pull(diff=patch()))

        (listed,) = list_app_updates()["items"]
        found = get_app_update(listed["key"])

        self.assertEqual((found["app"], found["number"], found["containers"]), ("app", 7, ["example-box:app"]))
        self.assertEqual([item["id"] for item in found["fixes"]], ["GHSA-high"])

    def test_the_cost_does_not_grow_with_the_updates_open(self):
        def arrange(size):
            arranged(*(pull(number, diff=patch()) for number in range(1, size + 1)))

        assert_cost_does_not_grow(self, sizes=(1, 8), arrange=arrange, draw=lambda _size: app_updates(), rows=len)
