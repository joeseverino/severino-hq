"""The compose change HQ writes for a container's unmet checks, and the help on
every container action item: a button, the exact config, or a stated reason."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.markup_testing import copied, elements
from hq.platform.core.testing import operator

from ..attention import infrastructure
from ..container_standard import STANDARD
from ..containers import containers
from ..derivation_testing import uncached
from ..findings import finding_key, rule_of
from ..pages import command_reach
from .test_container_standard import KEPT, on_the_host_network, runtime
from .test_containers import container, estate, inventory

CHECKS = {check.id: check for check in STANDARD}
SOCKET = {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}


class HardeningTests(TestCase):
    def hardening(self, name="web", **fields):
        estate()
        inventory("portainer.runtime", [runtime(name, **fields)])
        return next(item.hardening for item in containers() if item.running.name == name)

    def nothing(self, found):
        return not (found.changes or found.removals or found.unwritten)

    def test_a_container_meeting_every_check_is_given_nothing(self):
        self.assertTrue(self.nothing(self.hardening(**KEPT)))

    def test_what_was_not_read_is_given_nothing(self):
        estate()

        self.assertTrue(self.nothing(next(item.hardening for item in containers() if item.running.name == "web")))

    def test_only_the_unmet_check_gets_a_line(self):
        found = self.hardening(**{**KEPT, "security_opt": ["label=disable"]})

        (change,) = found.changes
        self.assertEqual(change.key, "security_opt")
        self.assertEqual(change.checks, ("no-new-privileges",))
        self.assertIn('"label=disable"', change.yaml)
        self.assertTrue(found.step(CHECKS["no-new-privileges"]).value.startswith("services:\n  web:\n"))

    def test_every_hardening_gap_is_written_for_its_own_service(self):
        found = self.hardening(
            service="frontend",
            network_mode="bridge",
            port_bindings=[
                {"container_port": "80/tcp", "host_ip": "0.0.0.0", "host_port": "8080"},
                {"container_port": "80/tcp", "host_ip": "::", "host_port": "8080"},
            ],
        )

        self.assertEqual(
            [change.key for change in found.changes], ["security_opt", "user", "ports", "mem_limit", "healthcheck"]
        )
        for change in found.changes:
            (check,) = change.checks
            self.assertTrue(found.step(CHECKS[check]).value.startswith("services:\n  frontend:\n"), check)
        self.assertIn('"127.0.0.1:8080:80"', found.step(CHECKS["ports-bound"]).value)
        self.assertEqual(found.removals, ())
        self.assertTrue(all(change.caution for change in found.changes))

    def test_reach_over_the_machine_is_written_or_removed(self):
        found = self.hardening(
            **{
                **KEPT,
                "privileged": True,
                "pid_mode": "host",
                "cap_add": ["SYS_ADMIN", "CHOWN"],
                "security_opt": ["seccomp=unconfined", "no-new-privileges:true"],
                "devices": ["/dev/ttyUSB0"],
                "mounts": [
                    {"type": "bind", "source": "/etc", "destination": "/host-etc", "read_only": False},
                    {"type": "bind", "source": "/etc/localtime", "destination": "/etc/localtime", "read_only": True},
                    {"type": "volume", "source": "web_data", "destination": "/data", "read_only": False},
                ],
            }
        )

        changes = {change.key: change for change in found.changes}
        self.assertEqual(set(changes), {"privileged", "security_opt", "volumes", "cap_drop"})
        self.assertEqual(changes["privileged"].yaml, "privileged: false")
        self.assertEqual(changes["security_opt"].yaml, 'security_opt:\n  - "no-new-privileges:true"')
        self.assertEqual(changes["security_opt"].checks, ("confined",))
        self.assertEqual(
            changes["volumes"].yaml,
            'volumes:\n  - "/etc:/host-etc:ro"\n  - "/etc/localtime:/etc/localtime:ro"\n  - "web_data:/data"',
        )
        self.assertEqual(changes["cap_drop"].yaml, 'cap_drop:\n  - "ALL"\ncap_add:\n  - "CHOWN"')
        self.assertIn("SYS_ADMIN", changes["cap_drop"].caution)
        self.assertEqual(
            {removal.check: removal.lines for removal in found.removals},
            {"own-process-namespace": ("pid: host",), "no-devices": ("devices: /dev/ttyUSB0",)},
        )
        for check in (
            "not-privileged",
            "confined",
            "no-system-path-writable",
            "no-powerful-capability",
            "own-process-namespace",
            "no-devices",
        ):
            self.assertTrue(found.covers(check), check)

    def test_unconfined_options_are_dropped_and_the_rest_kept(self):
        found = self.hardening(**{**KEPT, "security_opt": ["apparmor=unconfined"]})

        self.assertEqual([change.key for change in found.changes], ["security_opt"])
        self.assertEqual(found.changes[0].checks, ("confined", "no-new-privileges"))
        self.assertEqual(found.changes[0].yaml, 'security_opt:\n  - "no-new-privileges:true"')

        found = self.hardening(**{**KEPT, "security_opt": ["no-new-privileges:true", "seccomp=unconfined"]})

        self.assertEqual(found.changes[0].yaml, 'security_opt:\n  - "no-new-privileges:true"')

    def test_a_non_root_user_names_the_data_to_hand_over(self):
        found = self.hardening(
            **{
                **KEPT,
                "user": "root",
                "mounts": [
                    {"type": "bind", "source": "/opt/example/data", "destination": "/data", "read_only": False},
                ],
            }
        )

        (change,) = found.changes
        self.assertEqual(change.yaml, 'user: "1000:1000"')
        self.assertIn("chown -R 1000:1000 /opt/example/data", change.caution)

    def test_a_low_port_on_the_host_network_under_no_new_privileges_keeps_root_with_one_capability(self):
        found = on_the_host_network(user="", cap_drop=["ALL"], cap_add=["NET_BIND_SERVICE", "CHOWN", "SYS_ADMIN"])

        fix = found.hardening.step(CHECKS["not-root"]).value
        self.assertIn('cap_drop:\n      - "ALL"\n    cap_add:\n      - "NET_BIND_SERVICE"', fix)
        self.assertNotIn("user", fix)
        self.assertNotIn("CHOWN", fix)
        # The one change meets the capability check too, written once.
        self.assertEqual([change.key for change in found.hardening.changes], ["cap_drop"])

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
        self.assertIn("100m", change.caution)

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

    def test_the_host_network_is_removed_with_what_it_leaves_unknown(self):
        found = self.hardening(**{**KEPT, "network_mode": "host", "port_bindings": []})

        (removal,) = found.removals
        self.assertEqual(removal.lines, ("network_mode: host",))
        self.assertTrue(removal.caution)

    def test_the_socket_is_a_reason_not_a_change(self):
        found = self.hardening(**{**KEPT, "mounts": [SOCKET]})

        self.assertEqual((found.changes, found.removals), ((), ()))
        (gap,) = found.unwritten
        self.assertEqual(gap.check, "no-docker-socket")

    def test_a_declared_socket_holder_is_given_nothing(self):
        container("web", holds_docker_socket=True)

        self.assertTrue(self.nothing(self.hardening(**{**KEPT, "mounts": [SOCKET]})))


class PageTests(TestCase):
    def setUp(self):
        operator(self.client, "owner", email="owner@example.test")

    def test_each_finding_on_the_container_page_carries_its_own_fix(self):
        estate()
        container("web")
        inventory("portainer.runtime", [runtime("web", stack="web", **{**KEPT, "user": "", "mounts": [SOCKET]})])
        inventory(
            "portainer.compose_project",
            [
                {
                    "host": "example-box",
                    "name": "web",
                    "connection_ref": "example-portainer",
                    "config_files": ["/opt/example/web/docker-compose.yml"],
                },
            ],
        )

        page = self.client.get(reverse("control_plane:detail", args=["example-box-web"]))

        (root,) = [row for row in elements(page, "tr") if "Runs as root" in row.words]
        fixes = [copied(block) for block in elements(page, "pre") if block.inside(root)]
        under = " ".join(element.words for element in elements(page, "strong") if element.inside(root))
        self.assertEqual(fixes, ['services:\n  web:\n    user: "1000:1000"'])
        self.assertIn("/opt/example/web/docker-compose.yml", under)
        self.assertContains(page, command_reach("infrastructure.resource.update", "example-box-web")[0])


class FindingHelpTests(TestCase):
    """Each container finding carries its help: a button, a read link, or the steps by hand."""

    def items(self):
        """Each item by its rule and each container it names: its part, or
        for an image's item every container that runs it."""

        with uncached():
            return {
                (rule_of(finding_key(item)), name): item
                for item in tuple(infrastructure())
                if rule_of(finding_key(item))
                for name in [value for label, value in item.evidence if label == "Container"] or [item.part]
            }

    def remedies(self, item):
        return list(item.presses)

    def test_the_socket_item_offers_to_mark_a_watched_container_and_to_adopt_one_unwatched(self):
        estate()
        container("web")
        inventory(
            "portainer.runtime",
            [
                runtime("web", **{**KEPT, "mounts": [SOCKET]}),
                runtime("kuma", **{**KEPT, "mounts": [SOCKET]}),
            ],
        )
        items = self.items()

        (mark,) = self.remedies(items["container-hardening", "web"])
        self.assertEqual(mark.url, command_reach("infrastructure.resource.update", "example-box-web")[0])
        self.assertEqual((mark.capability, mark.target), ("infrastructure.resource.update", "example-box-web"))
        (adopt,) = self.remedies(items["container-hardening", "kuma"])
        self.assertEqual(adopt.method, "POST")
        self.assertIn("/adopt/record/portainer.container/", adopt.url)

    def test_the_socket_button_opens_the_update_of_its_own_declaration(self):
        operator(self.client, "owner", email="owner@example.test")
        estate()
        container("web")
        inventory("portainer.runtime", [runtime("web", **{**KEPT, "mounts": [SOCKET]})])
        (mark,) = self.remedies(self.items()["container-hardening", "web"])

        page = self.client.get(mark.url)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["resource"].key, "example-box-web")

    def test_every_other_reach_item_carries_its_compose_change(self):
        estate()
        container("web")
        inventory("portainer.runtime", [runtime("web", **{**KEPT, "privileged": True})])

        item = self.items()["container-hardening", "web"]

        self.assertIn("privileged: false", next(step.value for step in item.steps if step.kind == "do"))
        self.assertIn(item.subject_link.url, {link.url for link in item.pages})
