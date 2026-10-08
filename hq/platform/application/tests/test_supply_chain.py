from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..containers import DECLARED, LABEL, PROVENANCE, REGISTRY, VULNERABLE, containers
from ..security import cli_principal
from ..standards import MET, UNAVAILABLE, UNMET
from .test_containers import estate, inventory
from .test_upgrades import TARGET, declare, published

APP = "ghcr.io/example/app@sha256:aaa"
APP_TARGET = f"ghcr.io/example/app@{TARGET}"


def attested(key, *, packages=("pkg:npm/a@1",), source="", revision="abc1234", unread=""):
    record = {"digest": key, "image": key.partition("@")[0], "packages": list(packages), "sbom": "SPDX" if packages else ""}
    if source or revision:
        record["provenance"] = {"format": "SLSA 1.0", "source": source, "revision": revision,
                                "builder": "", "finished_at": "2026-09-20T00:00:00+00:00", "materials": ["pkg:docker/alpine@3.23"]}
    if unread:
        record = {"digest": key, "unread": unread}
    return {**record, "read_at": timezone.now().isoformat()}


def found(id_, *, severity="high", fixed=("1.2.1",), package="a"):
    return {"id": id_, "package": package, "installed": "1", "fixed": list(fixed), "severity": severity,
            "summary": "", "url": f"https://osv.dev/vulnerability/{id_}", "modified": "m"}


def checked(key, findings):
    return {"digest": key, "checked": 1, "findings": list(findings), "read_at": timezone.now().isoformat()}


def item(name):
    return next(each for each in containers() if each.running.name == name)


class SourceTests(TestCase):
    def test_what_an_image_is_built_from_is_known_in_order_of_trust(self):
        estate()
        # The label names example/app; its provenance names another.
        inventory("registry.digest", [attested(APP, source="https://github.com/example/fork")])

        self.assertEqual((item("app").standing.upstream, item("app").standing.source_from), ("example/app", LABEL))

        ManagedResource.objects.create(
            key="example-box-app", kind="portainer.container",
            spec={"connection_ref": "example-portainer", "host": "example-box", "name": "app", "source": "https://github.com/example/declared"},
        )
        self.assertEqual((item("app").standing.upstream, item("app").standing.source_from), ("example/declared", DECLARED))

    def test_provenance_names_the_source_of_an_image_that_does_not(self):
        estate()
        inventory("portainer.image", [
            {"connection_ref": "example-portainer", "host": "example-box", "id": "sha256:w",
             "tags": ["example/web:1.0.0"], "digests": ["example/web@sha256:web"],
             "containers": [{"container": "web", "reference": "example/web:1.0.0"}]},
        ])
        inventory("registry.digest", [attested("docker.io/example/web@sha256:web", source="git@github.com:example/web.git")])

        standing = item("web").standing
        self.assertEqual((standing.upstream, standing.source_from), ("example/web", PROVENANCE))
        self.assertEqual(standing.commit_url, "https://github.com/example/web/commit/abc1234")
        self.assertEqual(standing.built_on, ("alpine:3.23",))

    def test_an_image_in_githubs_registry_is_known_by_its_registry_last(self):
        estate()
        inventory("registry.image", [{"image": "ghcr.io/example/app", "tags": ["v1.2.0"], "read_at": timezone.now().isoformat()}])

        self.assertEqual(item("app").standing.source_from, REGISTRY)

    def test_the_declared_source_must_be_a_github_repository(self):
        from hq.domains.control_plane.provider_adapters.portainer import PortainerContainerSpec
        from pydantic import ValidationError

        base = {"connection_ref": "p", "host": "h", "name": "n"}
        self.assertEqual(PortainerContainerSpec(**base, source="https://github.com/example/app/").source, "https://github.com/example/app")
        with self.assertRaises(ValidationError):
            PortainerContainerSpec(**base, source="https://gitlab.example/owner/repo")


class VulnerabilityTests(TestCase):
    def test_only_a_serious_vulnerability_with_a_fix_makes_an_image_vulnerable(self):
        estate()
        inventory("registry.digest", [attested(APP)])
        inventory("registry.vulnerabilities", [checked(APP, [found("LOW", severity="low"), found("UNFIXED", fixed=())])])

        standing = item("app").standing
        self.assertNotEqual(standing.state, VULNERABLE)
        self.assertEqual(len(standing.findings), 2)

        inventory("registry.vulnerabilities", [checked(APP, [found("HIGH")])])
        standing = item("app").standing
        self.assertEqual((standing.state, standing.summary, standing.worst), (VULNERABLE, "1 serious vulnerability", "high"))


class StandardTests(TestCase):
    def test_the_supply_chain_is_measured_from_what_was_read(self):
        estate()
        inventory("registry.digest", [attested(APP)])
        inventory("registry.vulnerabilities", [checked(APP, [found("HIGH")])])

        chain = item("app").supply_chain

        self.assertEqual(chain.state_of("pinned"), MET)
        self.assertEqual(chain.state_of("source-known"), MET)
        self.assertEqual(chain.state_of("provenance"), MET)
        self.assertEqual(chain.state_of("sbom"), MET)
        self.assertEqual(chain.state_of("no-urgent"), UNMET)
        self.assertTrue(chain.serious)

    def test_what_was_not_read_is_unavailable_not_a_failure(self):
        estate()
        inventory("registry.digest", [attested(APP, unread="the registry refused an anonymous read")])

        chain = item("app").supply_chain

        self.assertEqual(chain.state_of("provenance"), UNAVAILABLE)
        self.assertEqual(chain.state_of("sbom"), UNAVAILABLE)
        self.assertEqual(chain.state_of("no-urgent"), UNAVAILABLE)


class UpgradeTests(TestCase):
    def plan(self, name):
        from ..upgrades import plans

        return next(plan for plan in plans() if plan.container.running.name == name)

    def test_an_upgrade_says_what_the_targets_packages_clear_and_bring(self):
        estate()
        published()
        declare("app")
        inventory("registry.digest", [attested(APP), attested(APP_TARGET)])
        inventory("registry.vulnerabilities", [
            checked(APP, [found("CLEARED"), found("KEPT", severity="low")]),
            checked(APP_TARGET, [found("KEPT", severity="low"), found("BROUGHT", severity="critical")]),
        ])

        plan = self.plan("app")

        self.assertEqual([each["id"] for each in plan.fixes], ["CLEARED"])
        self.assertEqual([each["id"] for each in plan.introduces], ["BROUGHT"])
        self.assertIn("target-affected", [blocker.id for blocker in plan.blockers])

    def test_a_target_is_vetted_by_what_its_publisher_attached(self):
        estate()
        published()
        declare("app")
        inventory("registry.digest", [attested(APP), attested(APP_TARGET, packages=(), revision="", source="")])

        reasons = {blocker.id for blocker in self.plan("app").not_automatic}

        self.assertLessEqual({"no-provenance", "no-package-list"}, reasons)
        self.assertNotIn("target-unread", reasons)


class CadenceTests(TestCase):
    def test_a_digest_is_read_once_and_a_failure_is_retried_later(self):
        from ..public_registry import RETRY_AFTER, _due

        now = timezone.now()
        read = {"digest": "x", "read_at": (now - timedelta(days=400)).isoformat()}
        failed = {"digest": "x", "unread": "down", "read_at": now.isoformat()}

        self.assertFalse(_due(read, None, now, force=True))
        self.assertFalse(_due(failed, None, now, force=False))
        self.assertTrue(_due(failed, None, now + RETRY_AFTER + timedelta(seconds=1), force=False))
        self.assertTrue(_due(None, None, now, force=False))

    def test_a_vulnerability_detail_is_read_once_until_osv_modifies_it(self):
        from ..public_registry import vulnerability_reader

        estate()
        inventory("registry.digest", [attested(APP)])
        held = {("GHSA-1", "a", "1"): found("GHSA-1")}
        with mock.patch("hq.platform.application.osv.matches", return_value=(1, {"pkg:npm/a@1": [("GHSA-1", "m")]})), \
                mock.patch("hq.platform.application.osv.detail", side_effect=AssertionError("read again")):
            record = vulnerability_reader(held)(APP)

        self.assertEqual([each["id"] for each in record["findings"]], ["GHSA-1"])
        self.assertEqual(record["unresolved"], [])

    def test_details_past_the_budget_are_left_for_the_next_run(self):
        from ..public_registry import vulnerability_reader

        estate()
        inventory("registry.digest", [attested(APP)])
        with mock.patch("hq.platform.application.osv.matches", return_value=(1, {"pkg:npm/a@1": [("A", "m"), ("B", "m")]})), \
                mock.patch("hq.platform.application.osv.detail", return_value={"id": "A"}):
            record = vulnerability_reader({}, budget=1)(APP)

        self.assertEqual(record["unresolved"], ["B"])


class RegistryReadTests(TestCase):
    def sweep(self):
        from ..sweep import record_sweep

        with self.captureOnCommitCallbacks(execute=True):
            record_sweep({"portainer.container": {"ok": True, "records": list(ProviderInventory.objects.get(kind="portainer.container").records)}},
                         principal=cli_principal())

    def test_a_sweep_that_finds_a_digest_hq_has_not_read_starts_the_read(self):
        estate()
        with mock.patch("hq.platform.application.scheduled_work.start") as start:
            self.sweep()
        start.assert_called_once_with("registry.refresh")

    def test_a_sweep_that_finds_nothing_new_is_silent(self):
        from ..public_registry import registry_due

        estate()
        inventory("registry.digest", [attested(APP), attested("docker.io/example/kuma@sha256:bbb")])
        self.assertFalse(registry_due())
        with mock.patch("hq.platform.application.scheduled_work.start") as start:
            self.sweep()
        start.assert_not_called()


class ContainerJoinTests(TestCase):
    def test_every_reading_that_names_a_container_joins_to_it(self):
        from ..relationships import relationships_for

        estate()
        declare("web")
        declare("app")
        inventory("portainer.network", [
            {"connection_ref": "example-portainer", "host": "example-box", "name": "shop_default", "driver": "bridge",
             "subnets": ["172.20.0.0/16"], "containers": ["web", "app"]},
        ])

        found = relationships_for("resource:example-box-web", principal=cli_principal())

        (network,) = found.group("On network").items
        self.assertEqual((network.entity.label, network.entity.detail), ("shop_default", "bridge · 172.20.0.0/16"))
        self.assertEqual(found.labels("Talks to"), ("app",))

    def test_the_container_page_shows_what_it_does_not_say_elsewhere(self):
        estate()
        declare("web")
        inventory("portainer.network", [
            {"connection_ref": "example-portainer", "host": "example-box", "name": "shop_default", "driver": "bridge", "containers": ["web"]},
        ])
        inventory("portainer.volume", [
            {"connection_ref": "example-portainer", "host": "example-box", "type": "bind", "source": "/opt/apps/web/data",
             "used_by": [{"container": "web", "destination": "/data"}]},
        ])
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

        page = self.client.get(reverse("control_plane:detail", args=["example-box-web"]))

        self.assertContains(page, "On network")
        self.assertContains(page, "shop_default")
        self.assertNotIn("Holds data in", page.context["view"].relationships.phrases())
