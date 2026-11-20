"""What the container standard's examples (``container_standard.EXAMPLES``,
walked in test_standards) cannot say: what a container's declaration excuses,
and the standard as an agent and Needs you read it."""

from django.test import TestCase

from ..container_standard import EXAMPLES
from ..containers import containers
from ..standards import INTENDED, Standing
from .test_containers import HIGH, container, estate, inventory


def runtime(container="web", **fields):
    return {"connection_ref": "example-portainer", "host": "example-box", "container": container, **fields}


KEPT = dict(EXAMPLES.kept)


class StandardTests(TestCase):
    def posture(self, name="web", **fields):
        estate()
        inventory("portainer.runtime", [runtime(name, **fields)])
        return next(item.posture for item in containers() if item.running.name == name)

    def test_a_check_declared_by_design_is_shown_with_its_reason_and_never_a_gap(self):

        container(by_design={"own-network": "It answers DNS on the host's port 53."})

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

        socket = {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "destination": "/var/run/docker.sock",
            "read_only": True,
        }
        container(holds_docker_socket=True)

        self.assertEqual(self.posture(**{**KEPT, "mounts": [socket]}).state_of("no-docker-socket"), Standing.MET)

    def test_a_declared_socket_holder_may_write_dockers_own_data_but_nothing_else(self):

        socket = {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "destination": "/var/run/docker.sock",
            "read_only": True,
        }
        volumes = {
            "type": "bind",
            "source": "/var/lib/docker/volumes",
            "destination": "/var/lib/docker/volumes",
            "read_only": False,
        }
        etc = {"type": "bind", "source": "/etc", "destination": "/host-etc", "read_only": False}
        container(holds_docker_socket=True)

        self.assertEqual(
            self.posture(**{**KEPT, "mounts": [socket, volumes]}).state_of("no-system-path-writable"), Standing.MET
        )
        self.assertEqual(
            self.posture(**{**KEPT, "mounts": [socket, volumes, etc]}).state_of("no-system-path-writable"),
            Standing.UNMET,
        )


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
        unmet = [check for check in found["posture"]["checks"] if check["state"] == "unmet"]
        self.assertIn("not-privileged", [check["id"] for check in unmet])
        self.assertTrue(all(check["fix"] and check["why"] for check in unmet))

    def test_an_unknown_address_is_not_found(self):
        from ..resources import get_resource

        estate()
        with self.assertRaises(Exception) as caught:
            get_resource("containers", "example-box:nothing", principal=self.principal())
        self.assertIn("NotFound", type(caught.exception).__name__)
