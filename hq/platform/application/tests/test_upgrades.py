import re

from django.test import SimpleTestCase, TestCase, tag
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource

from ..upgrades import HIGH as HIGH_RISK, LOW, MAJOR, PATCH, change_between, plans
from .test_container_standard import runtime
from .test_containers import HIGH, estate, inventory

TARGET = "sha256:" + "9" * 64


def declare(name):
    ManagedResource.objects.create(
        key=f"example-box-{name}",
        kind="portainer.container",
        spec={"connection_ref": "example-portainer", "host": "example-box", "name": name},
    )


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
        declare("app")

        plan = self.plan("app")

        self.assertEqual((plan.change, plan.risk, plan.target_digest), (PATCH, LOW, TARGET))
        self.assertEqual([advisory["id"] for advisory in plan.fixes], ["GHSA-high"])
        self.assertEqual(plan.verified_by, ("its health check",))
        # Nothing was snapshotted, so only the override comes back.
        self.assertEqual(plan.steps[-1].detail, "The override as it was is restored if verification fails.")
        # Nothing can apply it until a machine has the helper, and it says so.
        self.assertEqual([blocker.id for blocker in plan.blockers], ["no-apply-path"])

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
        declare("app")

        plan = self.plan("app")

        self.assertEqual([mount["source"] for mount in plan.data], ["app_data"])
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
        declare("app")

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
        declare("app")

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
        declare("app")
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
        declare("web")
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


class HelperInstallTests(TestCase):
    def test_while_the_helper_is_what_stands_in_the_way_the_plan_says_how_to_install_it(self):
        from ..upgrades import HELPER, SUDOERS

        estate()
        published()
        declare("app")

        plan = next(plan for plan in plans() if plan.container.running.name == "app")

        # HQ does not run on this machine, so its deploys never bring the helper.
        self.assertFalse(plan.install_brought)
        (fetch, install, allow, check) = plan.install
        self.assertEqual((fetch.id, install.id), ("fetch", "install"))
        self.assertIn(f"-o root -g root -m 0755 hq-host {HELPER}", install.detail)
        # A rule for the one program and only an upgrade's arguments, never a broader one.
        self.assertIn(f"'{HELPER} upgrade *'", allow.detail)
        self.assertIn(f"visudo -cf {SUDOERS}", allow.detail)
        # The account is not something HQ knows: the step says so, and cannot run as written.
        self.assertIn("HQ does not know which account", allow.label)
        self.assertIn('id -u "$account"', allow.detail)
        self.assertTrue(check.detail.startswith(f"sudo -n -l {HELPER} upgrade --operation "))

    def test_on_the_machine_hq_deploys_to_only_the_rule_is_needed(self):
        from ..upgrades import install_steps

        self.assertEqual([step.id for step in install_steps("example-box", deployed_here=True)], ["allow", "check"])

    def test_the_sudo_rule_admits_the_helpers_upgrade_and_nothing_else(self):
        from ..upgrades import HELPER, sudoers_command

        self.assertEqual(sudoers_command(), f"{HELPER} upgrade *")


class HelperCopyTests(TestCase):
    """The helper on a machine HQ does not deploy to: taken from the release HQ
    runs, by its signed digest, and refused on another platform."""

    def test_elsewhere_hq_host_comes_from_the_signed_release_hq_runs(self):
        from types import SimpleNamespace
        from unittest import mock

        from django.test import override_settings

        from ..upgrades import SIGNER_ISSUER, install_steps

        release = "ghcr.io/example/hq/composition@sha256:" + "b" * 64
        running = SimpleNamespace(running=SimpleNamespace(image=release))
        with (
            override_settings(SEVERINO_HQ_SOURCE="https://github.com/example/hq"),
            mock.patch("hq.platform.application.hq_self.own_container", return_value=running),
        ):
            fetch, install, *_ = install_steps("example-edge")

        self.assertIn(f"image={release} ", fetch.detail)
        # Verified against the compose workflow's identity before anything is pulled.
        self.assertIn(f"cosign verify --certificate-oidc-issuer {SIGNER_ISSUER}", fetch.detail)
        self.assertIn(r"example/hq/\.github/workflows/compose\.yml@refs/heads/main$", fetch.detail)
        self.assertLess(fetch.detail.index("cosign verify"), fetch.detail.index("docker pull"))
        # The copy is the verified digest or nothing, and only for the machine's own platform.
        self.assertIn('docker create --pull never "$image"', fetch.detail)
        self.assertIn('[ "$built" = "$here" ]', fetch.detail)
        self.assertLess(fetch.detail.index('"$built" = "$here"'), fetch.detail.index("docker create"))
        self.assertIn("example-edge runs $here", fetch.detail)
        self.assertIn("install -o root -g root -m 0755 hq-host", install.detail)

    def test_a_release_hq_cannot_name_by_digest_gives_no_command(self):
        from types import SimpleNamespace
        from unittest import mock

        from django.test import override_settings

        from ..upgrades import install_steps

        for image, source in (
            ("ghcr.io/example/hq/composition:latest", "https://github.com/example/hq"),
            ("ghcr.io/example/hq/composition@sha256:" + "b" * 64, ""),
        ):
            running = SimpleNamespace(running=SimpleNamespace(image=image))
            with (
                self.subTest(image=image, source=source),
                override_settings(SEVERINO_HQ_SOURCE=source),
                mock.patch("hq.platform.application.hq_self.own_container", return_value=running),
            ):
                fetch, *_ = install_steps("example-edge")
                self.assertIn("HQ cannot name the signed image it runs", fetch.detail)
                self.assertNotIn("docker create", fetch.detail)


@tag("source")
class HelperInstallSourceTests(SimpleTestCase):
    """The helper's sudo rule and signer as the Go program holds them."""

    @staticmethod
    def _source(path):
        from pathlib import Path

        return (Path(__file__).resolve().parents[4] / path).read_text()

    def test_the_helper_is_hq_hosts_upgrade_at_the_stacks_root_the_rule_names(self):
        from ..upgrades import STACKS_ROOT

        self.assertIn('if args[0] == "upgrade" {', self._source("controller/cmd/hq-host/main.go"))
        # The helper holds the stacks root the rule names; neither moves alone.
        self.assertIn(f'StacksRoot = "{STACKS_ROOT}"', self._source("controller/host/upgrade/upgrade.go"))

    def test_the_signer_is_the_one_a_deploy_checks(self):
        from ..upgrades import SIGNER_ISSUER, signer

        deploy = self._source("controller/host/deploy.go")
        self.assertIn(f'signerIssuer = "{SIGNER_ISSUER}"', deploy)
        (suffix,) = re.findall(r"QuoteMeta\(string\(s\)\) \+ `([^`]+)`", deploy)
        self.assertTrue(signer("https://github.com/example/hq").endswith(suffix))


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
