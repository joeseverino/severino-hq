from django.test import TestCase
from django.utils import timezone

from ..upgrades import MAJOR, PATCH, change_between, plans
from .test_container_standard import runtime
from .test_containers import container, estate, inventory

TARGET = "sha256:" + "9" * 64


def published(app_tags=("v1.2.0", "v1.2.1"), digests=None):
    now = timezone.now().isoformat()
    inventory(
        "registry.image",
        [
            {
                "image": "ghcr.io/example/app",
                "tags": list(app_tags),
                "digests": {"v1.2.1": TARGET} if digests is None else digests,
                "source": "https://github.com/example/app",
                "read_at": now,
            },
            {"image": "docker.io/example/web", "tags": ["1.0.0"], "read_at": now},
            {"image": "docker.io/example/kuma", "tags": ["1", "2"], "digests": {"2": TARGET}, "read_at": now},
        ],
    )


class ChangeTests(TestCase):
    def test_the_first_number_that_differs_is_the_size_of_the_move(self):
        self.assertEqual(change_between("v1.2.0", "v1.2.1"), PATCH)
        self.assertEqual(change_between("1.31.3-alpine", "1.32.0-alpine"), "minor")
        self.assertEqual(change_between("1", "2"), MAJOR)
        self.assertEqual(change_between("latest", "2"), "unknown")


class PlanTests(TestCase):
    def plan(self, name):
        return next(plan for plan in plans() if plan.container.running.name == name)

    def test_hq_itself_is_never_planned_as_an_upgrade(self):
        from .test_github_estate import store

        store(repository="example/app", url="https://github.com/example/app")
        estate()
        published()

        self.assertIn("own-pipeline", {blocker.id for blocker in self.plan("app").blockers})

    def test_an_agent_reads_the_plan_as_the_page_would(self):
        from ..resources import get_resource, list_resource
        from ..security import cli_principal

        estate()
        published()
        inventory("portainer.runtime", [runtime("app", healthcheck=True)])
        container("app")

        listed = list_resource("upgrades", {}, principal=cli_principal())
        found = get_resource("upgrades", "example-box:app", principal=cli_principal())

        self.assertEqual({item["address"] for item in listed["items"]}, {"example-box:app", "example-box:kuma"})
        self.assertEqual((found["to"]["digest"], found["change"], found["viable"]), (TARGET, "patch", True))


class ProvenanceJoinTests(TestCase):
    def test_a_container_knows_its_project_its_repository_and_what_is_open(self):
        from hq.domains.projects.models import Project

        from ..resources import get_resource
        from ..security import cli_principal
        from .test_github_estate import store

        store(
            repository="example/app",
            url="https://github.com/example/app",
            head={"sha": "b" * 40},
            deployments=[{"environment": "production", "sha": "a" * 40}],
            pull_requests=[
                {"number": 159, "title": "Next", "url": "https://github.com/example/app/pull/159", "draft": True}
            ],
        )
        Project.objects.create(name="App", slug="app", repository_url="https://github.com/example/app")
        estate()
        published()

        found = get_resource("containers", "example-box:app", principal=cli_principal())

        self.assertEqual(found["project"]["slug"], "app")
        self.assertEqual(found["repository"]["name"], "example/app")
        self.assertTrue(found["repository"]["undeployed"])  # main moved past production
        self.assertEqual(found["repository"]["open_pull_requests"][0]["number"], 159)
