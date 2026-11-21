from dataclasses import replace

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest
from hq.platform.core.testing import operator

from ..contracts import Closed
from ..machines import Machine
from ..upgrades import HIGH as HIGH_RISK, LOW, MAJOR, PATCH, change_between, plans
from .procedures import NEXT_REVISION, Supply, manifest
from .test_container_standard import runtime
from .test_containers import HIGH, container, estate, inventory
from .test_image_findings import project

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


class DeployInputs(Closed):
    compose_project_name: str
    compose_project_upgrade_service: str
    compose_project_upgrade_image: str


class ReviewedDeploymentTests(TestCase):
    operation = "example.compose_deploy"
    image = f"ghcr.io/example/app:v1.2.1@{TARGET}"
    pin = {"project": "app", "file": "compose.yaml", "service": "web", "image": image}

    def setUp(self):
        estate()
        published()
        inventory("portainer.runtime", [runtime("app", stack="app", service="web", healthcheck=True)])
        inventory("portainer.compose_project", [project("app", "/opt/example/app/compose.yaml")])
        container("app")
        operator(self.client)
        self.procedure = manifest(
            id=self.operation,
            input_type=DeployInputs,
            fixes=("container-image-update",),
            compose_updates={"source": "compose", "destination": "/opt/example"},
            compose_images=(self.pin,),
        )
        self.supply = Supply(self, self.procedure)
        self.supply.know(Machine("example-box"))

    def offer(self):
        from ..markup_testing import links

        response = self.client.get(reverse("action_items"))
        return [url for label, url in links(response.content) if label == "Review image update"]

    def test_reviewed_image_reuses_the_run_form_and_keeps_the_reviewed_values(self):
        (url,) = self.offer()
        response = self.client.get(url)
        self.assertContains(response, self.image)
        self.assertContains(response, "Deploy image")
        self.assertFalse(OperationRequest.objects.exists())
        response = self.client.post(
            url,
            {
                "parameters.compose_project_upgrade_image": "wrong",
                "reason": "Update the reviewed image",
                "__confirm_effect": "on",
                "__retry_key": "web:example-image-update",
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context_data", {}).get("form"))
        queued = OperationRequest.objects.get()
        self.assertEqual(queued.input["hosts"], ["example-box"])
        self.assertEqual(
            queued.input["parameters"],
            {
                "compose_project_name": "app",
                "compose_project_upgrade_service": "web",
                "compose_project_upgrade_image": self.image,
            },
        )
        self.assertRedirects(response, reverse("jobs:run", kwargs={"pk": queued.pk}))

    def test_stale_review_cannot_submit_through_the_old_link(self):
        (url,) = self.offer()
        self.supply(self.procedure.model_copy(update={"revision": NEXT_REVISION}))
        self.assertContains(self.client.post(url, {"__confirm_effect": "on"}), "Open Needs you", status_code=409)
        self.assertFalse(OperationRequest.objects.exists())

    def test_no_action_until_the_exact_pin_and_login_are_available(self):
        self.supply(self.procedure.model_copy(update={"compose_images": ()}))
        self.assertEqual(self.offer(), [])
        self.supply(self.procedure)
        self.supply.reach()
        self.assertEqual(self.offer(), [])


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
        self.assertEqual((plan.fixes_tone, plan.data_tone, plan.verified_tone), ("good", "", ""))
        # Nothing was snapshotted, so only the override comes back.
        self.assertEqual(plan.steps[-1].detail, "The override as it was is restored if verification fails.")
        # Nothing sends it to the machine, and the reason says that step, not
        # that the upgrade cannot happen.
        (blocker,) = plan.blockers
        self.assertEqual(blocker.id, "no-apply-path")
        self.assertEqual(blocker.reason, f"HQ does not send upgrades to {plan.container.machine.name}.")

    def test_data_is_snapshotted_and_restored(self):
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

        self.assertEqual(([mount["source"] for mount in plan.data], plan.data_tone), (["app_data"], "attention"))
        self.assertEqual([step.id for step in plan.steps][:2], ["pull", "snapshot"])
        self.assertIn("the snapshot", plan.steps[-1].detail)

    def test_a_major_version_is_high_risk_and_waits_for_a_person(self):
        estate()
        published()

        plan = self.plan("kuma")

        self.assertEqual((plan.change, plan.risk), (MAJOR, HIGH_RISK))
        self.assertIn("not-a-patch", [blocker.id for blocker in plan.not_automatic])

    def test_what_hq_cannot_act_on_blocks_it_by_name(self):
        estate()
        published(digests={})

        blockers = {blocker.id for blocker in self.plan("app").blockers}

        self.assertEqual(blockers, {"no-target-digest", "not-declared", "mounts-unread", "no-apply-path"})
        self.assertFalse(self.plan("app").viable)

    def test_nothing_is_automatic_before_its_target_is_read_and_opted_in(self):
        estate()
        published()
        inventory("portainer.runtime", [runtime("app", healthcheck=True)])
        container("app")

        reasons = {blocker.id for blocker in self.plan("app").not_automatic}

        self.assertLessEqual({"target-unread", "not-opted-in"}, reasons)

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
        self.assertEqual((found["to"]["digest"], found["change"], found["viable"]), (TARGET, "patch", False))
        self.assertEqual([step["id"] for step in found["steps"]][:2], ["pull", "trial"])


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

    def test_the_pin_step_names_the_override_it_would_write_never_the_compose_file(self):
        estate()
        published()
        inventory(
            "portainer.compose_project",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "name": "app",
                    "config_files": ["/opt/apps/app/docker-compose.yml"],
                    "containers": ["app"],
                },
            ],
        )

        plan = next(plan for plan in plans() if plan.container.running.name == "app")

        pin = next(step for step in plan.steps if step.id == "pin")
        self.assertIn("compose override", pin.label)
        self.assertTrue(pin.detail.startswith("/opt/apps/app/docker-compose.override.yml."))

    def test_the_pin_step_names_the_stacks_own_override_when_it_has_one(self):
        estate()
        published()
        inventory(
            "portainer.compose_project",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "name": "app",
                    "config_files": ["/opt/apps/app/compose.yaml", "/opt/apps/app/compose.override.yaml"],
                    "containers": ["app"],
                },
            ],
        )

        plan = next(plan for plan in plans() if plan.container.running.name == "app")

        pin = next(step for step in plan.steps if step.id == "pin")
        self.assertTrue(pin.detail.startswith("/opt/apps/app/compose.override.yaml."))


class ReadinessTests(TestCase):
    def test_a_current_container_says_what_an_upgrade_would_take_before_one_exists(self):
        from ..containers import containers
        from ..upgrades import readiness_of

        estate()
        published()
        container("web")
        inventory(
            "portainer.volume",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "type": "bind",
                    "source": "/opt/apps/web/data",
                    "used_by": [{"container": "web", "destination": "/data", "read_only": False}],
                },
            ],
        )

        found = readiness_of(next(item for item in containers() if item.running.name == "web"))

        self.assertEqual([mount["source"] for mount in found.data], ["/opt/apps/web/data"])
        self.assertEqual(found.verified_by, ())
        reasons = {blocker.id for blocker in found.not_automatic}
        self.assertLessEqual({"no-apply-path", "unverifiable", "attestations-unread", "not-opted-in"}, reasons)
        # Nothing about a target it does not have.
        self.assertFalse(reasons & {"no-target-digest", "not-a-patch", "target-affected"})
        self.assertFalse(found.automatic)
        self.assertEqual((found.verified_tone, found.automatic_tone), ("attention", ""))
        ready = replace(found, verified_by=("its health check",), not_automatic=())
        self.assertEqual((ready.verified_tone, ready.automatic_tone), ("good", "good"))


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
