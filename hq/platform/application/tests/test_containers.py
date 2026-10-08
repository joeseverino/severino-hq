from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..containers import BEHIND, CURRENT, UNKNOWN, VULNERABLE, attention, containers
from ..exposure import OPEN, PRIVATE, Exposure, RouteExposure, front_door_names, listening_ports


def exposed(level):
    """Every container reached at ``level``, through one synthetic name."""

    return mock.patch(
        "hq.platform.application.container_attention.exposure_of_container",
        return_value=Exposure((RouteExposure("app.example.com", level, "", ""),)),
    )


def inventory(kind, records):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={"records": records, "reachable": True, "connected": True, "observed_at": timezone.now()},
    )


def running(name, image, host="example-box"):
    return {
        "name": name, "stack": name, "image": image, "state": "running", "status": "Up 2 days",
        "host": host, "host_address": "", "connection_ref": "example-portainer", "ports": [],
        "network_mode": "bridge", "portainer_managed": False,
    }


def estate(*, advisories=(), app_tags=("v1.2.0", "v1.3.0")):
    now = timezone.now().isoformat()
    inventory("portainer.container", [
        running("app", "ghcr.io/example/app:v1.2.0@sha256:aaa"),
        running("web", "example/web:1.0.0"),
        running("kuma", "example/kuma@sha256:bbb"),
    ])
    inventory("portainer.image", [
        {"connection_ref": "example-portainer", "host": "example-box", "id": "sha256:bbb",
         "tags": ["example/kuma:1"], "created_at": "2025-10-20T17:53:48+00:00",
         "containers": [{"container": "kuma", "reference": "example/kuma@sha256:bbb"}]},
    ])
    inventory("registry.image", [
        {"image": "ghcr.io/example/app", "tags": list(app_tags), "source": "https://github.com/example/app", "read_at": now},
        {"image": "docker.io/example/web", "tags": ["1.0.0"], "read_at": now},
        {"image": "docker.io/example/kuma", "tags": ["1", "2"], "read_at": now},
    ])
    inventory("registry.upstream", [
        {"repository": "example/app", "url": "https://github.com/example/app", "read_at": now,
         "releases": [{"tag": "v1.3.0", "url": "https://github.com/example/app/releases/v1.3.0", "published_at": now}],
         "advisories": list(advisories)},
    ])


HIGH = {"id": "GHSA-high", "severity": "high", "summary": "s", "url": "u", "vulnerabilities": [["< 1.2.1", "1.2.1"]]}


class StandingTests(TestCase):
    def standings(self):
        return {item.running.name: item.standing for item in containers()}

    def test_each_container_is_judged_by_the_version_it_runs(self):
        estate(advisories=[HIGH, {**HIGH, "id": "GHSA-old", "vulnerabilities": [["< 1.0.0", "1.0.0"]]}])

        found = self.standings()

        self.assertEqual(found["app"].state, VULNERABLE)
        self.assertEqual([item["id"] for item in found["app"].advisories], ["GHSA-high"])
        self.assertEqual(found["app"].newer, ("v1.3.0",))
        self.assertEqual(found["app"].release["url"], "https://github.com/example/app/releases/v1.3.0")
        self.assertEqual(found["web"].state, CURRENT)
        # Digest-only: the tag it was pulled as comes from the machine's copy.
        self.assertEqual((found["kuma"].tag, found["kuma"].state, found["kuma"].newer), ("1", BEHIND, ("2",)))
        self.assertEqual(containers()[0].running.name, "app")  # worst first

    def test_an_image_with_no_source_is_behind_but_never_called_safe_from_advisories(self):
        estate()

        found = self.standings()

        self.assertEqual((found["kuma"].source, found["app"].source), ("", "https://github.com/example/app"))

    def test_a_registry_that_was_not_read_leaves_the_standing_unknown(self):
        estate()
        ProviderInventory.objects.filter(kind="registry.image").delete()

        self.assertEqual(self.standings()["web"].state, UNKNOWN)

    def test_an_advisory_is_one_item_per_version_and_updates_are_one_item(self):
        estate(advisories=[HIGH])

        with exposed(OPEN):
            items = {item.key: item for item in attention()}

        self.assertEqual(set(items), {"container-advisory:ghcr.io/example/app:v1.2.0", "container-updates"})
        advisory = items["container-advisory:ghcr.io/example/app:v1.2.0"]
        self.assertEqual((advisory.status, advisory.magnitude), ("serious", 1))
        self.assertIn("Fixed in 1.2.1", advisory.body)
        # The body is the images. What to do is a step, and why HQ does not
        # do it is one sentence that names no machine.
        self.assertEqual(items["container-updates"].body, "example/kuma:1 → 2.")
        steps = items["container-updates"].workflow.steps
        self.assertEqual(
            [(step.phase, step.summary) for step in steps],
            [
                ("do", "On example-box, set the image of kuma to docker.io/example/kuma:2 where it is defined, "
                       "then start it again."),
                ("cannot", "HQ cannot apply an update itself yet."),
            ],
        )

    def test_an_update_names_the_command_when_its_compose_project_was_read(self):
        estate()
        inventory("portainer.runtime", [
            {"connection_ref": "example-portainer", "host": "example-box", "container": "kuma", "service": "kuma-web"},
        ])
        inventory("portainer.compose_project", [
            {"connection_ref": "example-portainer", "host": "example-box", "name": "kuma",
             "config_files": ["/opt/apps/kuma/compose.yaml", "/opt/apps/kuma/compose.override.yaml"]},
        ])

        (item,) = [item for item in attention() if item.key == "container-updates"]

        (step,) = [step for step in item.workflow.steps if step.phase == "run"]
        self.assertEqual(
            (step.phase, step.label, step.summary),
            (
                "run",
                "On example-box, set the image of kuma to docker.io/example/kuma:2 in "
                "/opt/apps/kuma/compose.override.yaml, then run",
                'ssh example-box "sudo docker compose -f /opt/apps/kuma/compose.yaml '
                '-f /opt/apps/kuma/compose.override.yaml up -d kuma-web"',
            ),
        )

    def test_an_update_card_says_each_thing_once(self):
        """One reason, no machine named twice, nothing about how HQ would apply it."""

        estate(advisories=[HIGH])

        for item in attention():
            if item.workflow is None:
                continue
            said = " ".join([item.body, *(step.summary for step in item.workflow.steps)])
            with self.subTest(item=item.key):
                self.assertEqual(said.count("cannot apply"), 1)
                self.assertNotIn("sudo rule", said)
                self.assertNotIn("helper", said)

    def test_many_advisories_are_one_thing_to_do(self):
        estate(advisories=[HIGH, {**HIGH, "id": "GHSA-high-2"}, {**HIGH, "id": "GHSA-high-3"}])

        (advisory,) = [item for item in attention() if item.key.startswith("container-advisory:")]

        self.assertEqual(advisory.magnitude, 1)

    def test_an_advisory_no_release_fixes_yet_is_said_not_raised(self):
        estate(advisories=[HIGH], app_tags=("v1.2.0",))

        with exposed(OPEN):
            (advisory,) = [item for item in attention() if item.key.startswith("container-advisory:")]

        self.assertEqual(advisory.status, "attention")
        self.assertIn("no release fixes it yet", advisory.title)

    def test_an_advisory_is_as_urgent_as_what_reaches_it(self):
        estate(advisories=[HIGH])

        def status():
            (advisory,) = [item for item in attention() if item.key.startswith("container-advisory:")]
            return advisory.status, advisory.body

        with exposed(OPEN):
            opened = status()
        with exposed(PRIVATE):
            private = status()
        # No routing kind is read here, so HQ does not know what reaches it.
        unknown = status()
        with mock.patch("hq.platform.application.paths.reads_every_route", return_value=True):
            unrouted = status()

        self.assertEqual(opened[0], "serious")
        self.assertIn("open to the internet as app.example.com", opened[1])
        self.assertEqual(private[0], "attention")
        # A gap in what HQ read is not evidence nothing reaches it.
        self.assertEqual(unknown[0], "serious")
        self.assertIn("not known who can reach it", unknown[1])
        # Nothing routes to it, and HQ read everything that could: information.
        self.assertEqual(unrouted[0], "neutral")
        self.assertIn("not reachable", unrouted[1])

    def test_nothing_known_asks_for_nothing(self):
        estate(app_tags=("v1.2.0",))
        inventory("registry.image", [
            {"image": "ghcr.io/example/app", "tags": ["v1.2.0"], "read_at": timezone.now().isoformat()},
            {"image": "docker.io/example/web", "tags": ["1.0.0"], "read_at": timezone.now().isoformat()},
            {"image": "docker.io/example/kuma", "tags": ["1"], "read_at": timezone.now().isoformat()},
        ])

        self.assertEqual(attention(), ())


class RefreshTests(TestCase):
    def test_the_upstreams_read_are_the_ones_the_images_name(self):
        from hq.platform.application.public_registry import refresh
        from hq.platform.application.security import cli_principal

        inventory("portainer.container", [running("app", "example/app:1.0.0"), running("hub", "ghcr.io/example/hub:2")])
        read = {
            "docker.io/example/app": {"image": "docker.io/example/app", "tags": ["1.0.0"], "source": "https://github.com/example/app-src"},
            "ghcr.io/example/hub": {"image": "ghcr.io/example/hub", "tags": ["2"], "source": ""},
        }
        upstreams = []

        def upstream(repository):
            upstreams.append(repository)
            return {"repository": repository, "releases": [], "advisories": []}

        with (
            mock.patch("hq.platform.application.public_registry.read_image", side_effect=lambda name, references: read[name]),
            mock.patch("hq.platform.application.public_registry.read_upstream", side_effect=upstream),
            mock.patch("hq.platform.application.public_registry._configured", return_value=False),
        ):
            refresh(principal=cli_principal(), force=True)

        self.assertEqual(sorted(upstreams), ["example/app-src", "example/hub"])
        stored = ProviderInventory.objects.get(kind="registry.image").records
        self.assertEqual(sorted(record["image"] for record in stored), sorted(read))


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))

    def test_the_page_leads_with_what_needs_you(self):
        estate(advisories=[HIGH])

        response = self.client.get(reverse("control_plane:containers"))

        needs = [group["standing"].label for group in response.context["needs"]]
        self.assertEqual(needs, ["ghcr.io/example/app:v1.2.0", "example/kuma:1"])
        self.assertContains(response, "GHSA-high")
        self.assertEqual((response.context["pinned"], response.context["matched"]), (2, 1))

    def test_a_version_nothing_can_fix_and_nothing_public_reaches_does_not_need_you(self):
        estate(advisories=[HIGH], app_tags=("v1.2.0",))

        with exposed(PRIVATE):
            response = self.client.get(reverse("control_plane:containers"))

        needs = [group["standing"].label for group in response.context["needs"]]
        self.assertNotIn("ghcr.io/example/app:v1.2.0", needs)
        # Still counted where the page says what is known.
        self.assertEqual(response.context["vulnerable"], 1)
        with exposed(OPEN):
            response = self.client.get(reverse("control_plane:containers"))
        self.assertIn("ghcr.io/example/app:v1.2.0", [group["standing"].label for group in response.context["needs"]])

    def test_nothing_is_called_current_before_a_registry_answers(self):
        inventory("portainer.container", [running("web", "example/web:1.0.0")])

        response = self.client.get(reverse("control_plane:containers"))

        self.assertNotContains(response, "All up to date")
        self.assertContains(response, "Unknown")

    def test_all_current_only_when_every_container_was_read(self):
        estate(app_tags=("v1.2.0",))
        inventory("portainer.container", [running("web", "example/web:1.0.0")])

        response = self.client.get(reverse("control_plane:containers"))

        self.assertContains(response, "All up to date")

    def test_a_compose_file_copied_to_start_it_is_not_where_it_is_defined(self):
        from ..containers import _compose_files

        inventory("portainer.compose_project", [
            {"host": "example-box", "name": "app", "connection_ref": "example-portainer",
             "config_files": ["/run/app-compose.X1/next.yml", "/opt/apps/app/docker-compose.yml"]},
        ])

        self.assertEqual(_compose_files()[("example-box", "app")], ("/opt/apps/app/docker-compose.yml",))

    def test_a_declared_container_links_to_its_own_page_and_shows_its_standing(self):
        estate()
        ManagedResource.objects.create(
            key="example-box-kuma", kind="portainer.container",
            spec={"connection_ref": "example-portainer", "host": "example-box", "name": "kuma"},
        )

        listing = self.client.get(reverse("control_plane:containers"))
        detail = self.client.get(reverse("control_plane:detail", args=["example-box-kuma"]))

        self.assertContains(listing, reverse("control_plane:detail", args=["example-box-kuma"]))
        self.assertContains(detail, "<code>2</code> available", html=False)
        self.assertContains(detail, "Cannot check")

    def test_a_machine_shows_each_container_standing_as_the_containers_page_does(self):
        estate(advisories=[HIGH])

        response = self.client.get(reverse("control_plane:machine", args=["example-box"]))

        self.assertEqual(response.context["standings"]["kuma"].latest, "2")
        self.assertContains(response, "2 available")
        self.assertContains(response, "1 known")

    def test_a_container_running_an_image_by_id_stays_listed_and_says_why(self):
        inventory("portainer.container", [running("orphan", "sha256:0123456789abcdef0123")])

        (item,) = containers()

        self.assertEqual(item.standing.state, UNKNOWN)
        self.assertIn("by id only", item.standing.unread)

    def test_nothing_running_says_so(self):
        response = self.client.get(reverse("control_plane:containers"))

        self.assertContains(response, "No containers yet")

    def test_it_needs_a_sign_in(self):
        self.client.logout()

        self.assertEqual(self.client.get(reverse("control_plane:containers")).status_code, 302)


class QueryBudgetTests(TestCase):
    """The page's cost is its readings, never its rows: every container's
    standing shares one read of each reading."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))

    def queries_for(self, count):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        estate()
        inventory("portainer.container", [running(f"app-{index}", "ghcr.io/example/app:v1.2.0") for index in range(count)])
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(reverse("control_plane:containers")).status_code, 200)
        return len(captured)

    def test_the_cost_does_not_grow_with_the_containers(self):
        self.assertEqual(self.queries_for(3), self.queries_for(30))


class RetryTests(TestCase):
    def test_an_unresolved_digest_is_read_again_on_the_next_refresh(self):
        from hq.platform.application.public_registry import refresh
        from hq.platform.application.security import cli_principal

        inventory("portainer.container", [running("app", "example/app:1.0.0")])
        answers = iter([
            {"image": "docker.io/example/app", "tags": ["1.0.0", "1.0.1"], "digests": {"1.0.1": ""}, "unresolved": ["1.0.1"]},
            {"image": "docker.io/example/app", "tags": ["1.0.0", "1.0.1"], "digests": {"1.0.1": "sha256:" + "1" * 64}, "unresolved": []},
        ])
        with (
            mock.patch("hq.platform.application.public_registry.read_image", side_effect=lambda name, references: next(answers)),
            mock.patch("hq.platform.application.public_registry.read_upstream", side_effect=AssertionError),
            mock.patch("hq.platform.application.public_registry._configured", return_value=False),
        ):
            refresh(principal=cli_principal(), force=True)
            refresh(principal=cli_principal(), force=True)

        (record,) = ProviderInventory.objects.get(kind="registry.image").records
        self.assertEqual((record["digests"]["1.0.1"], record["unresolved"]), ("sha256:" + "1" * 64, []))


class FrontDoorTests(TestCase):
    """A proxy is on no route, since routes pass through it; it is as exposed
    as the worst route that enters its machine."""

    def item(self, *, ports=(), network_mode="bridge", exposed_ports=(), machine="example-box"):
        running = mock.Mock(ports=tuple(ports), network_mode=network_mode)
        running.name = "proxy"
        box = mock.Mock()
        box.name = machine
        return mock.Mock(running=running, machine=box, runtime={"exposed_ports": list(exposed_ports)})

    def entering(self, names):
        return mock.patch(
            "hq.platform.application.exposure.routed_containers",
            return_value={("example-box", ""): frozenset(names)},
        )

    def test_the_container_publishing_the_front_door_takes_in_its_routes(self):
        with self.entering({"app.example.com"}):
            self.assertEqual(front_door_names(self.item(ports=(80, 443))), {"app.example.com"})

    def test_on_the_host_network_the_image_says_what_it_listens_on(self):
        host = self.item(network_mode="host", exposed_ports=(80, 81, 443))

        self.assertEqual(listening_ports(host), (80, 81, 443))
        with self.entering({"app.example.com"}):
            self.assertEqual(front_door_names(host), {"app.example.com"})

    def test_a_container_off_the_front_door_takes_in_nothing(self):
        with self.entering({"app.example.com"}):
            self.assertEqual(front_door_names(self.item(ports=(9000,))), frozenset())
            self.assertEqual(front_door_names(self.item(network_mode="host", exposed_ports=(53,))), frozenset())
            self.assertEqual(front_door_names(self.item(ports=(443,), machine="other-box")), frozenset())
