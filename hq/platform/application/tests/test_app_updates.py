from django.test import TestCase

from ..app_updates import app_updates
from .test_container_standard import runtime
from .test_containers import HIGH, estate, inventory
from .test_github_estate import store
from .test_upgrades import TARGET, published

RUNNING = "ghcr.io/example/app:v1.2.0@sha256:aaa"
PINNED = f"ghcr.io/example/app:v1.2.1@{TARGET}"
# The login the reading says HQ's update run opens pull requests as.
UPDATER = "hq-example[bot]"
SERIOUS = {"id": "CVE-new", "package": "libexample", "severity": "critical", "fixed": ["2.0"], "url": "u"}


def moved(before=RUNNING, after=PINNED, service="app"):
    """One service's image as the reading says an update moves it."""

    return {"service": service, "before": before, "after": after}


def pull(number=7, app="app", moves=(), read=True):
    """Renovate's own pull request from the repository itself, changing ``app``'s Compose file alone."""

    file = {"path": f"compose/{app}/compose.yaml", "status": "modified", "additions": 1, "deletions": 1, "app": app}
    return {
        "number": number,
        "title": f"Update ghcr.io/example/app Docker tag to v1.2.1 ({number})",
        "url": f"https://github.com/example/alpha/pull/{number}",
        "author": UPDATER,
        "head": "f" * 40,
        "head_repository": "example/alpha",
        "branch": f"renovate/app-{number}",
        "checks": {"state": "success", "total": 1, "failing": [], "running": 0},
        "files": [file],
        "changed_files": 1,
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
    store(compose=["compose/app/compose.yaml"], updater=UPDATER, pull_requests=list(pulls))


class AppUpdateTests(TestCase):
    def update(self):
        (found,) = app_updates()
        return found

    def test_a_serious_finding_the_new_pin_brings_is_named(self):
        arranged(pull(moves=[moved()]), target_findings=[SERIOUS])

        found = self.update()

        self.assertEqual([item["id"] for item in found.brings], ["CVE-new"])
        self.assertEqual(found.brings_said.tone, "serious")
