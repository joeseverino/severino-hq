"""What the container standard's examples (``container_standard.EXAMPLES``,
walked in test_standards) cannot say: what a container's declaration excuses,
and the standard as an agent and Needs you read it."""

from django.test import TestCase

from ..container_standard import EXAMPLES
from ..containers import containers
from ..standards import INTENDED, Standing
from .test_containers import HIGH, estate, inventory


def runtime(container="web", **fields):
    return {"connection_ref": "example-portainer", "host": "example-box", "container": container, **fields}


KEPT = dict(EXAMPLES.kept)


def on_the_host_network(*, nameservers=(), **fields):
    """The web container on its machine's network, listening on ports 53 and 3000."""
    from .test_containers import running
    from .test_estate import device, devices

    estate()
    devices(device("example-box", addresses=["100.64.0.5"]), nameservers=nameservers)
    inventory("portainer.container", [running("web", "example/web:1.0.0") | {"network_mode": "host"}])
    on_host = {**KEPT, "network_mode": "host", "port_bindings": [], "exposed_ports": [53, 3000], **fields}
    inventory("portainer.runtime", [runtime("web", **on_host)])
    return next(item for item in containers() if item.running.name == "web")


class StandardTests(TestCase):
    def test_root_with_only_the_capability_its_low_port_takes_is_root_by_necessity(self):
        held = on_the_host_network(cap_drop=["ALL"], cap_add=["NET_BIND_SERVICE"], user="").posture
        wider = on_the_host_network(cap_drop=["ALL"], cap_add=["CHOWN"], user="").posture

        self.assertEqual(held.state_of("not-root"), INTENDED)
        self.assertEqual(wider.state_of("not-root"), Standing.UNMET)


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
        unmet = [check for check in found["posture"]["checks"] if check["state"] == "unmet"]
        self.assertIn("not-privileged", [check["id"] for check in unmet])
        self.assertTrue(all(check["fix"] and check["why"] for check in unmet))
