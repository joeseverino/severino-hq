from django.test import TestCase

from ..containers import containers
from ..standards import INTENDED, MET, UNAVAILABLE, UNMET
from .test_containers import HIGH, estate, inventory


def runtime(container="web", **fields):
    return {"connection_ref": "example-portainer", "host": "example-box", "container": container, **fields}


# How a well-run container reads: a user, no-new-privileges, a limit, a check.
KEPT = {
    "user": "1000", "security_opt": ["no-new-privileges:true"], "memory_limit": 268435456,
    "healthcheck": True, "network_mode": "bridge",
    "port_bindings": [{"container_port": "80/tcp", "host_ip": "127.0.0.1", "host_port": "8080"}],
}


class StandardTests(TestCase):
    def posture(self, name="web", **fields):
        estate()
        inventory("portainer.runtime", [runtime(name, **fields)])
        return next(item.posture for item in containers() if item.running.name == name)

    def test_a_well_run_container_meets_every_check(self):
        found = self.posture(**KEPT)

        self.assertEqual(found.unmet, ())
        self.assertFalse(found.serious)

    def test_reach_over_the_machine_is_serious(self):
        found = self.posture(**{
            **KEPT, "privileged": True, "pid_mode": "host", "cap_add": ["SYS_ADMIN"],
            "security_opt": ["seccomp=unconfined"],
            "mounts": [
                {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True},
                {"type": "bind", "source": "/etc", "destination": "/host-etc", "read_only": False},
            ],
        })

        serious = {result.check.id for result in found.unmet if result.check.serious}
        self.assertEqual(serious, {
            "not-privileged", "no-docker-socket", "own-process-namespace", "confined",
            "no-system-path-writable", "no-powerful-capability",
        })

    def test_a_check_declared_by_design_is_shown_with_its_reason_and_never_a_gap(self):
        from hq.domains.control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="example-box-web", kind="portainer.container",
            spec={"connection_ref": "example-portainer", "host": "example-box", "name": "web",
                  "by_design": {"own-network": "It answers DNS on the host's port 53."}},
        )

        found = self.posture(**{**KEPT, "network_mode": "host", "user": "root"})

        self.assertEqual(found.state_of("own-network"), INTENDED)
        (intended,) = found.intended
        self.assertEqual(intended.reason, "It answers DNS on the host's port 53.")
        # What it did not declare is still a gap.
        self.assertEqual([result.check.id for result in found.unmet], ["not-root"])

    def test_by_design_names_real_checks_and_says_why(self):
        from pydantic import ValidationError

        from hq.domains.control_plane.provider_adapters.portainer import PortainerContainerSpec

        base = {"connection_ref": "example-portainer", "host": "example-box", "name": "web"}
        with self.assertRaises(ValidationError):
            PortainerContainerSpec(**base, by_design={"not-a-check": "because"})
        with self.assertRaises(ValidationError):
            PortainerContainerSpec(**base, by_design={"own-network": "  "})
        self.assertEqual(
            PortainerContainerSpec(**base, by_design={"own-network": " DNS "}).by_design, {"own-network": "DNS"}
        )

    def test_a_container_declared_to_hold_the_socket_is_not_flagged_for_it(self):
        from hq.domains.control_plane.models import ManagedResource

        socket = {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}
        ManagedResource.objects.create(
            key="example-box-web", kind="portainer.container",
            spec={"connection_ref": "example-portainer", "host": "example-box", "name": "web", "holds_docker_socket": True},
        )

        self.assertEqual(self.posture(**{**KEPT, "mounts": [socket]}).state_of("no-docker-socket"), MET)

    def test_a_declared_socket_holder_may_write_dockers_own_data_but_nothing_else(self):
        from hq.domains.control_plane.models import ManagedResource

        socket = {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}
        volumes = {"type": "bind", "source": "/var/lib/docker/volumes", "destination": "/var/lib/docker/volumes", "read_only": False}
        etc = {"type": "bind", "source": "/etc", "destination": "/host-etc", "read_only": False}
        ManagedResource.objects.create(
            key="example-box-web", kind="portainer.container",
            spec={"connection_ref": "example-portainer", "host": "example-box", "name": "web", "holds_docker_socket": True},
        )

        self.assertEqual(self.posture(**{**KEPT, "mounts": [socket, volumes]}).state_of("no-system-path-writable"), MET)
        self.assertEqual(self.posture(**{**KEPT, "mounts": [socket, volumes, etc]}).state_of("no-system-path-writable"), UNMET)

    def test_an_undeclared_container_writing_dockers_data_is_still_flagged(self):
        volumes = {"type": "bind", "source": "/var/lib/docker/volumes", "destination": "/v", "read_only": False}

        self.assertEqual(self.posture(**{**KEPT, "mounts": [volumes]}).state_of("no-system-path-writable"), UNMET)

    def test_a_container_not_declared_to_hold_the_socket_still_is(self):
        socket = {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}

        self.assertEqual(self.posture(**{**KEPT, "mounts": [socket]}).state_of("no-docker-socket"), UNMET)

    def test_a_read_only_system_mount_and_a_data_bind_are_not_reach(self):
        found = self.posture(**{**KEPT, "mounts": [
            {"type": "bind", "source": "/etc/localtime", "destination": "/etc/localtime", "read_only": True},
            {"type": "bind", "source": "/opt/apps/web/data", "destination": "/data", "read_only": False},
        ]})

        self.assertEqual(found.state_of("no-system-path-writable"), MET)

    def test_an_applications_own_runtime_directory_is_not_the_machines(self):
        found = self.posture(**{**KEPT, "mounts": [
            {"type": "bind", "source": "/run/web", "destination": "/run/web", "read_only": False},
        ]})
        whole = self.posture(**{**KEPT, "mounts": [
            {"type": "bind", "source": "/run", "destination": "/host-run", "read_only": False},
        ]})

        self.assertEqual(found.state_of("no-system-path-writable"), MET)
        self.assertEqual(whole.state_of("no-system-path-writable"), UNMET)

    def test_hardening_is_not_serious(self):
        found = self.posture(user="", network_mode="host", port_bindings=[{"host_ip": "0.0.0.0", "host_port": "80"}])

        self.assertEqual(
            {result.check.id for result in found.unmet},
            {"not-root", "no-new-privileges", "own-network", "ports-bound", "memory-limited", "health-checked"},
        )
        self.assertFalse(found.serious)

    def test_what_was_not_read_is_unavailable_not_a_failure(self):
        estate()

        found = next(item.posture for item in containers() if item.running.name == "web")

        self.assertEqual(found.state_of("not-privileged"), UNAVAILABLE)
        self.assertEqual(found.state_of("no-docker-socket"), UNAVAILABLE)


class ResourceTests(TestCase):
    def principal(self):
        from ..security import cli_principal

        return cli_principal()

    def test_an_agent_reads_what_the_page_shows(self):
        from ..resources import get_resource, list_resource

        estate(advisories=[HIGH])
        inventory("portainer.runtime", [runtime("app", privileged=True, user="0")])

        listed = list_resource("containers", {"limit": 10}, principal=self.principal())
        found = get_resource("containers", "example-box:app", principal=self.principal())

        self.assertEqual(listed["items"][0]["address"], "example-box:app")  # worst first
        self.assertEqual(found["standing"]["state"], "vulnerable")
        self.assertEqual(found["image"]["digest"], "sha256:aaa")
        self.assertTrue(found["runtime"]["privileged"])
        self.assertIn("not-privileged", [item["id"] for item in found["posture"]["unmet"]])
        self.assertTrue(all(item["fix"] for item in found["posture"]["unmet"]))

    def test_an_unknown_address_is_not_found(self):
        from ..resources import get_resource

        estate()
        with self.assertRaises(Exception) as caught:
            get_resource("containers", "example-box:nothing", principal=self.principal())
        self.assertIn("NotFound", type(caught.exception).__name__)


class AttentionTests(TestCase):
    def test_reach_over_a_machine_is_one_queued_item_per_check_and_hardening_is_not_queued(self):
        from ..containers import attention

        estate()
        inventory("portainer.runtime", [
            runtime("web", privileged=True, user=""),
            runtime("kuma", privileged=True, user=""),
        ])

        keys = {item.key: item for item in attention()}

        self.assertIn("container-posture:not-privileged", keys)
        self.assertEqual(keys["container-posture:not-privileged"].magnitude, 2)
        self.assertNotIn("container-posture:not-root", keys)


class HealthCheckedTests(TestCase):
    def check(self, runtime, *, check=None, serves=()):
        from types import SimpleNamespace as Row

        from ..container_standard import _health_checked

        return _health_checked(Row(runtime=runtime, running=Row(check=check), serves=serves))

    def test_its_own_health_check_or_a_name_hq_requests_says_whether_it_works(self):
        self.assertTrue(self.check({"healthcheck": True}))
        # An image with no shell cannot run a check; a name HQ requests still says.
        self.assertTrue(self.check({}, serves=("admin.example.com",)))
        self.assertFalse(self.check({}))
        self.assertIsNone(self.check(None))
