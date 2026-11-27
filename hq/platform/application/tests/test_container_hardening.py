"""The compose change HQ writes for a container's unmet checks, and the help on
every container action item: a button, the exact config, or a stated reason."""

from django.test import TestCase

from ..container_standard import STANDARD
from ..containers import containers
from .test_container_standard import KEPT, on_the_host_network, runtime
from .test_containers import container, estate, inventory

CHECKS = {check.id: check for check in STANDARD}


class HardeningTests(TestCase):
    def hardening(self, name="web", **fields):
        estate()
        inventory("portainer.runtime", [runtime(name, **fields)])
        return next(item.hardening for item in containers() if item.running.name == name)

    def test_a_non_root_user_is_offered_where_no_new_privileges_is_waived_or_the_port_is_its_own(self):
        container("web", by_design={"no-new-privileges": "Its program binds port 53 through a file capability."})
        waived = on_the_host_network(user="", cap_drop=["ALL"], security_opt=[]).hardening
        bridged = self.hardening(**{**KEPT, "user": "", "exposed_ports": [53]})

        self.assertIn(
            'user: "1000:1000"\n    cap_add:\n      - "NET_BIND_SERVICE"', waived.step(CHECKS["not-root"]).value
        )
        # In a network of its own any user binds a low port.
        self.assertNotIn("cap_add", bridged.step(CHECKS["not-root"]).value)

    def test_a_limit_is_derived_from_what_it_uses_when_that_was_read(self):
        found = self.hardening(**{**KEPT, "memory_limit": 0, "memory_usage": 100 * 1024 * 1024})

        (change,) = found.changes
        self.assertEqual(change.yaml, "mem_limit: 256m")

    def test_a_documented_health_endpoint_is_used_and_none_is_said(self):
        estate()
        inventory(
            "portainer.container",
            [
                {
                    "name": "grafana",
                    "stack": "grafana",
                    "image": "grafana/grafana:11.0.0",
                    "state": "running",
                    "status": "Up",
                    "host": "example-box",
                    "connection_ref": "example-portainer",
                    "ports": [],
                }
            ],
        )
        inventory(
            "portainer.runtime",
            [
                runtime("grafana", **{**KEPT, "healthcheck": False, "port_bindings": []}),
            ],
        )

        (item,) = containers()
        (change,) = item.hardening.changes
        self.assertIn("http://127.0.0.1:3000/api/health", change.yaml)

        found = self.hardening(**{**KEPT, "healthcheck": False, "port_bindings": []})

        self.assertEqual(found.changes, ())
        (gap,) = found.unwritten
        self.assertEqual(gap.check, "health-checked")
        self.assertTrue(gap.reason)
