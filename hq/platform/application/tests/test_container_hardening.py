"""The compose change HQ writes for a container's unmet checks, and the help on
every container action item: a button, the exact config, or a stated reason."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource

from ..action_links import command_url
from ..attention import infrastructure
from ..containers import containers
from ..derivation_testing import uncached
from ..exposure import OPEN, PRIVATE
from ..findings import rule_of
from .markup import select, text
from .test_container_standard import KEPT, runtime
from .test_containers import HIGH, estate, exposed, inventory

SOCKET = {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}


def declare(name="web", **spec):
    ManagedResource.objects.create(
        key=f"example-box-{name}",
        kind="portainer.container",
        spec={"connection_ref": "example-portainer", "host": "example-box", "name": name, **spec},
    )


class HardeningTests(TestCase):
    def hardening(self, name="web", **fields):
        estate()
        inventory("portainer.runtime", [runtime(name, **fields)])
        return next(item.hardening for item in containers() if item.running.name == name)

    def test_a_container_meeting_every_check_is_given_nothing(self):
        found = self.hardening(**KEPT)

        self.assertFalse(found.any)
        self.assertEqual(found.yaml, "")

    def test_what_was_not_read_is_given_nothing(self):
        estate()

        found = next(item.hardening for item in containers() if item.running.name == "web")

        self.assertFalse(found.any)

    def test_only_the_unmet_check_gets_a_line(self):
        found = self.hardening(**{**KEPT, "security_opt": ["label=disable"]})

        (change,) = found.changes
        self.assertEqual(change.key, "security_opt")
        self.assertEqual(change.checks, ("no-new-privileges",))
        self.assertIn('"label=disable"', change.yaml)
        self.assertTrue(found.yaml.startswith("services:\n  web:\n"))

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
            found.yaml,
            "services:\n"
            "  frontend:\n"
            "    security_opt:\n"
            '      - "no-new-privileges:true"\n'
            '    user: "1000:1000"\n'
            "    ports:\n"
            '      - "127.0.0.1:8080:80"\n'
            "    mem_limit: 512m\n"
            "    healthcheck:\n"
            '      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:80/ || exit 1"]\n'
            "      interval: 30s\n"
            "      timeout: 5s\n"
            "      retries: 3\n",
        )
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
        self.assertIsNone(found.socket_link)

    def test_a_declared_socket_holder_is_given_nothing(self):
        declare("web", holds_docker_socket=True)

        self.assertFalse(self.hardening(**{**KEPT, "mounts": [SOCKET]}).any)


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

    def test_the_container_page_shows_the_block_for_its_compose_file(self):
        estate()
        declare("web")
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

        self.assertTrue(select(page, "id", "hardening"))
        self.assertIn("/opt/example/web/docker-compose.yml", text(page))
        self.assertIn('user: "1000:1000"', text(page))
        self.assertContains(page, command_url("infrastructure.resource.update", "example-box-web"))

    def test_a_container_meeting_every_check_has_no_block(self):
        estate()
        declare("web")
        inventory("portainer.runtime", [runtime("web", **KEPT)])

        page = self.client.get(reverse("control_plane:detail", args=["example-box-web"]))

        self.assertFalse(select(page, "id", "hardening"))


class FindingHelpTests(TestCase):
    """Each container finding carries its help: a button, a read link, or the steps by hand."""

    def items(self):
        with uncached():
            return {(rule_of(item.key), item.part): item for item in infrastructure() if rule_of(item.key)}

    def remedies(self, item):
        return [action for step in item.workflow.steps for action in step.actions if action.name == "remedy"]

    def test_the_socket_item_offers_to_mark_a_watched_container_and_to_adopt_one_unwatched(self):
        estate()
        declare("web")
        inventory(
            "portainer.runtime",
            [
                runtime("web", **{**KEPT, "mounts": [SOCKET]}),
                runtime("kuma", **{**KEPT, "mounts": [SOCKET]}),
            ],
        )
        items = self.items()

        (mark,) = self.remedies(items["container-hardening", "web"])
        self.assertEqual(mark.url, command_url("infrastructure.resource.update", "example-box-web"))
        self.assertEqual((mark.capability, mark.target), ("infrastructure.resource.update", "example-box-web"))
        (adopt,) = self.remedies(items["container-hardening", "kuma"])
        self.assertEqual(adopt.method, "POST")
        self.assertIn("/adopt/record/portainer.container/", adopt.url)

    def test_the_socket_button_opens_the_update_prefilled_with_its_declaration(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))
        estate()
        declare("web")
        inventory("portainer.runtime", [runtime("web", **{**KEPT, "mounts": [SOCKET]})])
        (mark,) = self.remedies(self.items()["container-hardening", "web"])

        page = self.client.get(mark.url)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["form"].initial["__target"], "example-box-web")
        self.assertEqual(page.context["form"].initial["spec"]["name"], "web")

    def test_a_marked_container_leaves_the_item(self):
        estate()
        declare("web", holds_docker_socket=True)
        inventory("portainer.runtime", [runtime("web", **{**KEPT, "mounts": [SOCKET]})])

        self.assertNotIn(("container-hardening", "web"), self.items())

    def test_every_other_reach_item_links_its_container_to_its_compose_change(self):
        """The detector's own read link stands beside the machine's own."""

        estate()
        declare("web")
        inventory("portainer.runtime", [runtime("web", **{**KEPT, "privileged": True})])

        item = self.items()["container-hardening", "web"]

        self.assertTrue(any(link.url.endswith("/example-box-web/#hardening") for link in item.links))
        self.assertIn(item.subject.url, {link.url for link in item.links})

    def test_an_advisory_says_which_upgrade_clears_it_and_why_it_cannot_run_yet(self):
        estate(advisories=[HIGH])
        declare("app")

        item = self.items()["container-image-advisory", "app"]

        said = {step.phase: step.summary for step in item.workflow.steps}
        self.assertIn("ghcr.io/example/app:v1.3.0", said["do"])
        self.assertEqual(said["cannot"], "HQ cannot apply an update itself yet.")
        self.assertTrue(item.url.endswith("/example-box-app/#upgrade"))

    def test_a_watched_update_leads_with_its_upgrade_plan(self):
        estate()
        declare("kuma")

        item = self.items()["container-image-update", "kuma"]

        self.assertEqual(item.action, "Upgrade plan for kuma")
        self.assertTrue(item.url.endswith("/example-box-kuma/#upgrade"))
        self.assertIn(reverse("control_plane:containers"), [link.url for link in item.links])

    def test_an_unwatched_advisory_is_updated_by_hand_with_no_plan(self):
        estate(advisories=[HIGH])

        item = self.items()["container-image-advisory", "app"]

        # Adopting plans an upgrade but does not clear the advisory, so it is
        # no remedy here; the card still says how to move the image by hand.
        self.assertEqual(self.remedies(item), [])
        self.assertIn("ghcr.io/example/app:v1.3.0", {step.phase: step.summary for step in item.workflow.steps}["do"])
        self.assertFalse(any(link.url.endswith("#upgrade") for link in item.links))

    def test_a_private_advisory_no_release_fixes_is_not_an_action_item(self):
        estate(advisories=[HIGH], app_tags=("v1.2.0",))

        with exposed(PRIVATE):
            self.assertNotIn(("container-image-advisory", "app"), self.items())
        with exposed(OPEN):
            self.assertIn(("container-image-advisory", "app"), self.items())

    def test_an_advisory_no_release_fixes_offers_no_upgrade(self):
        estate(advisories=[HIGH], app_tags=("v1.2.0",))

        item = self.items()["container-image-advisory", "app"]

        self.assertEqual(self.remedies(item), [])
        self.assertFalse(any("#upgrade" in link.url for link in item.links))
