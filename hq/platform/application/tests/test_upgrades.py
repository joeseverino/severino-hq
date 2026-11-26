from django.test import TestCase
from django.utils import timezone

from ..upgrades import HIGH as HIGH_RISK, LOW, MAJOR, PATCH, change_between, plans
from .test_container_standard import runtime
from .test_containers import HIGH, container, estate, inventory

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


class DataTests(TestCase):
    def test_a_runtime_directory_is_never_data_to_keep(self):
        from ..upgrades import data_of

        kept = data_of(
            [
                {"type": "volume", "source": "app_data", "destination": "/data", "read_only": False},
                {"type": "bind", "source": "/opt/apps/app/conf", "destination": "/conf", "read_only": False},
                {"type": "bind", "source": "/run/app", "destination": "/run/app", "read_only": False},
                {"type": "bind", "source": "/var/run/app.pid", "destination": "/pid", "read_only": False},
                {"type": "bind", "source": "/tmp/scratch", "destination": "/scratch", "read_only": False},
            ]
        )

        self.assertEqual([mount["source"] for mount in kept], ["app_data", "/opt/apps/app/conf"])


class PlanTests(TestCase):
    def plan(self, name):
        return next(plan for plan in plans() if plan.container.running.name == name)

    def test_a_patch_that_can_be_verified_is_low_risk_and_fixes_what_it_says(self):
        estate(advisories=[{**HIGH, "vulnerabilities": [["< 1.2.1", "1.2.1"]]}])
        published()
        inventory("portainer.runtime", [runtime("app", healthcheck=True)])
        container("app")

        plan = self.plan("app")

        self.assertEqual((plan.change, plan.risk, plan.target_digest), (PATCH, LOW, TARGET))
        self.assertEqual([advisory["id"] for advisory in plan.fixes], ["GHSA-high"])
        self.assertEqual(plan.verified_by, ("its health check",))
        self.assertEqual((plan.fixes_said.tone, plan.data_said.tone, plan.verified_said.tone), ("good", "", ""))
        self.assertEqual(plan.blockers, ())

    def test_its_data_is_its_writable_mounts(self):
        estate()
        published()
        inventory(
            "portainer.runtime",
            [
                runtime(
                    "app",
                    healthcheck=True,
                    mounts=[
                        {"type": "volume", "source": "app_data", "destination": "/data", "read_only": False},
                        {
                            "type": "bind",
                            "source": "/etc/localtime",
                            "destination": "/etc/localtime",
                            "read_only": True,
                        },
                    ],
                )
            ],
        )
        container("app")

        plan = self.plan("app")

        self.assertEqual(([mount["source"] for mount in plan.data], plan.data_said.tone), (["app_data"], "attention"))

    def test_a_major_version_is_high_risk_and_waits_for_a_person(self):
        estate()
        published()

        plan = self.plan("kuma")

        self.assertEqual((plan.change, plan.risk), (MAJOR, HIGH_RISK))
        self.assertIn("not-a-patch", [blocker.id for blocker in plan.not_automatic])
        # The card says the size of the move beside its risk, so its reasons leave it out.
        self.assertNotIn("not-a-patch", [blocker.id for blocker in plan.waits_on])

    def test_what_hq_cannot_act_on_blocks_it_by_name(self):
        estate()
        published(digests={})

        blockers = {blocker.id for blocker in self.plan("app").blockers}

        self.assertEqual(blockers, {"no-target-digest", "not-declared", "mounts-unread"})
        self.assertFalse(self.plan("app").viable)

    def test_nothing_is_automatic_before_its_target_is_read(self):
        estate()
        published()
        inventory("portainer.runtime", [runtime("app", healthcheck=True)])
        container("app")

        reasons = {blocker.id for blocker in self.plan("app").waits_on}

        self.assertIn("target-unread", reasons)

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


class JoinedReadingTests(TestCase):
    """What a plan needs, taken from readings HQ already has."""

    def test_mounts_come_from_the_data_mount_reading_when_inspect_was_not_read(self):
        from ..containers import containers

        estate()
        published()
        container("app")
        inventory(
            "portainer.volume",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "type": "bind",
                    "source": "/opt/apps/app/data",
                    "used_by": [{"container": "app", "destination": "/data", "read_only": False}],
                },
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "type": "bind",
                    "source": "/var/run/docker.sock",
                    "used_by": [{"container": "app", "destination": "/var/run/docker.sock", "read_only": True}],
                },
            ],
        )

        plan = next(plan for plan in plans() if plan.container.running.name == "app")
        posture = next(item.posture for item in containers() if item.running.name == "app")

        self.assertEqual([mount["source"] for mount in plan.data], ["/opt/apps/app/data"])
        self.assertNotIn("mounts-unread", {blocker.id for blocker in plan.blockers})
        self.assertEqual(posture.state_of("no-docker-socket"), "unmet")


class UpgradeCardTests(TestCase):
    def page(self, name):
        from django.urls import reverse

        from hq.platform.core.testing import operator

        estate()
        published()
        container(name)
        operator(self.client)
        return self.client.get(reverse("control_plane:detail", args=[f"example-box-{name}"])).content.decode()

    def test_a_container_with_nothing_newer_draws_no_upgrade_card(self):
        self.assertNotIn('id="upgrade"', self.page("web"))

    def test_a_container_with_a_newer_release_draws_its_upgrade_card(self):
        self.assertIn('id="upgrade"', self.page("kuma"))


class ExposureOrderTests(TestCase):
    def test_what_the_internet_reaches_is_planned_first(self):
        from unittest import mock

        from ..exposure import OPEN, PRIVATE, Exposure, RouteExposure

        estate()
        published()

        def exposure(item):
            level = OPEN if item.running.name == "kuma" else PRIVATE
            return Exposure((RouteExposure(f"{item.running.name}.example.com", level, "", ""),))

        calm = [plan.container.running.name for plan in plans()]
        with mock.patch("hq.platform.application.exposure.exposure_of_container", side_effect=exposure):
            ranked = [plan.container.running.name for plan in plans()]

        self.assertEqual(calm[0], "app")  # least risky first, when exposure is equal
        self.assertEqual(ranked[0], "kuma")  # the open one first, though riskier
