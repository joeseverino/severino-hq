import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.public_registry import DIGEST_KIND as DIGEST
from hq.platform.core.testing import operator

from ..containers import DECLARED, LABEL, PROVENANCE, REGISTRY, VULNERABLE, containers
from ..report_testing import bridged
from ..security import cli_principal
from .network import Response, answering
from .test_containers import container, estate, inventory
from .test_upgrades import TARGET, published

APP = "ghcr.io/example/app@sha256:aaa"
APP_TARGET = f"ghcr.io/example/app@{TARGET}"


def attested(key, *, packages=("pkg:npm/a@1",), source="", revision="abc1234", unread=""):
    record = {
        "digest": key,
        "image": key.partition("@")[0],
        "packages": list(packages),
        "sbom": "SPDX" if packages else "",
    }
    if source or revision:
        record["provenance"] = {
            "format": "SLSA 1.0",
            "source": source,
            "revision": revision,
            "builder": "",
            "finished_at": "2026-09-20T00:00:00+00:00",
            "materials": ["pkg:docker/alpine@3.23"],
        }
    if unread:
        record = {"digest": key, "unread": unread}
    return {**record, "read_at": timezone.now().isoformat()}


def found(id_, *, severity="high", fixed=("1.2.1",), package="a"):
    return {
        "id": id_,
        "package": package,
        "installed": "1",
        "fixed": list(fixed),
        "severity": severity,
        "summary": "",
        "url": f"https://osv.dev/vulnerability/{id_}",
        "modified": "m",
    }


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

        container("app", source="https://github.com/example/declared")
        self.assertEqual(
            (item("app").standing.upstream, item("app").standing.source_from), ("example/declared", DECLARED)
        )

    def test_provenance_names_the_source_of_an_image_that_does_not(self):
        estate()
        inventory(
            "portainer.image",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "id": "sha256:w",
                    "tags": ["example/web:1.0.0"],
                    "digests": ["example/web@sha256:web"],
                    "containers": [{"container": "web", "reference": "example/web:1.0.0"}],
                },
            ],
        )
        inventory(
            "registry.digest", [attested("docker.io/example/web@sha256:web", source="git@github.com:example/web.git")]
        )

        standing = item("web").standing
        self.assertEqual((standing.upstream, standing.source_from), ("example/web", PROVENANCE))
        self.assertEqual(standing.commit_url, "https://github.com/example/web/commit/abc1234")
        self.assertEqual(standing.built_on, ("alpine:3.23",))

    def test_what_only_its_publisher_can_attach_is_a_fact_and_never_a_failed_check(self):
        estate()
        container("app")
        inventory("registry.digest", [attested(APP, packages=(), revision="")])
        operator(self.client)

        chain = item("app").supply_chain
        page = self.client.get(reverse("control_plane:detail", args=["example-box-app"]))

        self.assertEqual(chain.unmet, ())
        self.assertEqual(chain.met, chain.measured)
        self.assertContains(page, "None listed by its publisher")
        self.assertContains(page, "None from its publisher")

    def test_an_image_in_githubs_registry_is_known_by_its_registry_last(self):
        estate()
        inventory(
            "registry.image",
            [{"image": "ghcr.io/example/app", "tags": ["v1.2.0"], "read_at": timezone.now().isoformat()}],
        )

        self.assertEqual(item("app").standing.source_from, REGISTRY)

    def test_the_declared_source_must_be_a_github_repository(self):
        from pydantic import ValidationError

        from hq.domains.control_plane.provider_adapters.portainer import PortainerContainerSpec

        base = {"connection_ref": "p", "host": "h", "name": "n"}
        self.assertEqual(
            PortainerContainerSpec(**base, source="https://github.com/example/app/").source,
            "https://github.com/example/app",
        )
        with self.assertRaises(ValidationError):
            PortainerContainerSpec(**base, source="https://gitlab.example/owner/repo")


# OSV's answers for an image built from an untagged commit of its own
# module (a pseudo-version) with one vulnerable dependency, recorded from
# api.osv.dev with the module paths renamed: the querybatch answer for each
# package, and each record's detail. Each vulnerability is listed by OSV
# under its GHSA id and its GO id, the two naming each other as aliases.
MAIN = "pkg:golang/github.com/example/web@v0.0.0-20261118030823-d859bc092c89"
DEPENDENCY = "pkg:golang/github.com/example/dep/v2@v2.2.5"
RECORDED_MATCHES = {
    MAIN: [
        {"id": "GHSA-9mjw-79r6-c9m8", "modified": "2024-12-04T22:18:00Z"},
        {"id": "GO-2024-3172", "modified": "2024-10-09T20:59:24.410942Z"},
    ],
    DEPENDENCY: [
        {"id": "GHSA-p7v4-vr35-mj6f", "modified": "2026-10-19T21:41:22.608698004Z"},
        {"id": "GO-2026-6791", "modified": "2026-11-23T20:30:11.270670887Z"},
    ],
}
RECORDED_DETAILS = {
    "GHSA-9mjw-79r6-c9m8": {
        "id": "GHSA-9mjw-79r6-c9m8",
        "aliases": ["CVE-2024-33662", "GO-2024-3172"],
        "summary": "Improper use of an encryption algorithm in the AesEncrypt function",
        "modified": "2024-12-04T22:18:00Z",
        "database_specific": {"cwe_ids": ["CWE-326", "CWE-327"], "severity": "HIGH", "github_reviewed": True},
        "affected": [
            {
                "package": {"name": "github.com/example/web", "ecosystem": "Go"},
                "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "2.20.2"}]}],
            }
        ],
    },
    "GO-2024-3172": {
        "id": "GO-2024-3172",
        "aliases": ["CVE-2024-33662", "GHSA-9mjw-79r6-c9m8"],
        "summary": "Improper use of an encryption algorithm in the AesEncrypt function in github.com/example/web",
        "modified": "2024-10-09T20:59:24.410942Z",
        "database_specific": {"review_status": "UNREVIEWED", "url": "https://pkg.go.dev/vuln/GO-2024-3172"},
        "affected": [
            {
                "package": {"name": "github.com/example/web", "ecosystem": "Go"},
                "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}]}],
            }
        ],
    },
    "GHSA-p7v4-vr35-mj6f": {
        "id": "GHSA-p7v4-vr35-mj6f",
        "aliases": ["CVE-2026-95837", "GO-2026-6791"],
        "summary": "CRI plugin: Checkpoint restore bypasses destination security context",
        "modified": "2026-10-19T21:41:22.608698004Z",
        "database_specific": {"github_reviewed": True, "cwe_ids": ["CWE-269"], "severity": "HIGH"},
        "affected": [
            {
                "package": {"name": "github.com/example/dep/v2", "ecosystem": "Go"},
                "ranges": [{"type": "SEMVER", "events": [{"introduced": "2.1.0"}, {"fixed": "2.2.7"}]}],
            },
            {
                "package": {"name": "github.com/example/dep/v2", "ecosystem": "Go"},
                "ranges": [{"type": "SEMVER", "events": [{"introduced": "2.3.0"}, {"fixed": "2.3.4"}]}],
            },
        ],
    },
    "GO-2026-6791": {
        "id": "GO-2026-6791",
        "aliases": ["CVE-2026-95837", "GHSA-p7v4-vr35-mj6f"],
        "summary": "Checkpoint restore bypasses destination security context in github.com/example/dep/v2",
        "modified": "2026-11-23T20:30:11.270670887Z",
        "database_specific": {"url": "https://pkg.go.dev/vuln/GO-2026-6791", "review_status": "REVIEWED"},
        "affected": [
            {
                "package": {"name": "github.com/example/dep/v2", "ecosystem": "Go"},
                "ranges": [
                    {
                        "type": "SEMVER",
                        "events": [
                            {"introduced": "2.1.0"},
                            {"fixed": "2.2.7"},
                            {"introduced": "2.3.0"},
                            {"fixed": "2.3.4"},
                        ],
                    }
                ],
            }
        ],
    },
}


def osv_recorded(request):
    if request.method == "POST":
        queries = json.loads(request.content)["queries"]
        return Response(
            {"results": [{"vulns": RECORDED_MATCHES.get(query["package"]["purl"], [])} for query in queries]}
        )
    return Response(RECORDED_DETAILS[str(request.url).rpartition("/")[2]])


class VulnerabilityTests(TestCase):
    def test_an_image_of_its_own_untagged_commit_is_judged_by_what_a_release_can_fix(self):
        from ..public_registry import vulnerability_reader

        estate()
        web = "docker.io/example/web@sha256:web"
        inventory(
            "portainer.image",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "id": "sha256:w",
                    "tags": ["example/web:1.0.0"],
                    "digests": ["example/web@sha256:web"],
                    "containers": [{"container": "web", "reference": "example/web:1.0.0"}],
                },
            ],
        )
        inventory("registry.digest", [{**attested(web, packages=(MAIN, DEPENDENCY), revision=""), "main": [MAIN]}])
        with answering(osv_recorded):
            record = vulnerability_reader({})(web)
        inventory("registry.vulnerabilities", [{**record, "read_at": timezone.now().isoformat()}])

        standing = item("web").standing
        # Its own module is not matched against releases its commit is newer
        # than, and the dependency's one vulnerability is one finding.
        self.assertEqual([each["id"] for each in standing.findings], ["GHSA-p7v4-vr35-mj6f"])
        self.assertIn("GO-2026-6791", standing.findings[0]["aliases"])
        # The module its SBOM names is the source when the image names none.
        self.assertEqual((standing.upstream, standing.source_from), ("example/web", "sbom"))
        # No release fixes it, so it is said and never raised.
        self.assertEqual((standing.urgent, standing.state != VULNERABLE), ((), True))
        result = next(each for each in item("web").supply_chain.results if each.check.id == "no-urgent")
        self.assertTrue(result.met)
        self.assertIn(standing.unfixed_said, result.reading.evidence)

    def test_a_serious_vulnerability_is_urgent_once_the_newest_release_is_clear_of_it(self):
        estate()
        published()
        inventory("registry.digest", [attested(APP), attested(APP_TARGET)])

        def standing_with(target):
            inventory("registry.vulnerabilities", [checked(APP, [found("HIGH")]), *target])
            return item("app").standing, next(
                each for each in item("app").supply_chain.results if each.check.id == "no-urgent"
            )

        standing, result = standing_with([checked(APP_TARGET, [])])
        self.assertEqual((standing.state, standing.summary), (VULNERABLE, "1 serious vulnerability"))
        self.assertEqual(result.state, "unmet")

        standing, result = standing_with([checked(APP_TARGET, [found("HIGH")])])
        self.assertNotEqual(standing.state, VULNERABLE)
        self.assertTrue(result.met)

        standing, result = standing_with([])
        self.assertNotEqual(standing.state, VULNERABLE)
        self.assertEqual(result.state, "unavailable")


class UpgradeTests(TestCase):
    def plan(self, name):
        from ..upgrades import plans

        return next(plan for plan in plans() if plan.container.running.name == name)

    def test_an_upgrade_says_what_the_targets_packages_clear_and_bring(self):
        estate()
        published()
        container("app")
        inventory("registry.digest", [attested(APP), attested(APP_TARGET)])
        inventory(
            "registry.vulnerabilities",
            [
                checked(APP, [found("CLEARED"), found("KEPT", severity="low"), found("MINOR", severity="low")]),
                checked(APP_TARGET, [found("KEPT", severity="low"), found("BROUGHT", severity="critical")]),
            ],
        )

        plan = self.plan("app")

        # It fixes what the title counts, never a finding the title leaves out.
        self.assertEqual([each["id"] for each in plan.fixes], ["CLEARED"])
        self.assertEqual([each["id"] for each in plan.introduces], ["BROUGHT"])
        self.assertIn("target-affected", [blocker.id for blocker in plan.blockers])

    def test_a_target_is_vetted_by_what_its_publisher_attached(self):
        estate()
        published()
        container("app")
        inventory("registry.digest", [attested(APP), attested(APP_TARGET, packages=(), revision="", source="")])

        reasons = {blocker.id for blocker in self.plan("app").not_automatic}

        self.assertLessEqual({"no-provenance", "no-package-list"}, reasons)
        self.assertNotIn("target-unread", reasons)


class CadenceTests(TestCase):
    def test_a_digest_is_read_once_and_a_failure_is_retried_later(self):
        from ..freshness import RETRY_AFTER
        from ..public_registry import _due

        now = timezone.now()
        read = {"digest": "x", "read_at": (now - timedelta(days=400)).isoformat()}
        failed = {"digest": "x", "unread": "down", "read_at": now.isoformat()}

        self.assertFalse(_due(read, DIGEST, now, force=True))
        self.assertFalse(_due(failed, DIGEST, now, force=False))
        self.assertTrue(_due(failed, DIGEST, now + RETRY_AFTER + timedelta(seconds=1), force=False))
        self.assertTrue(_due(None, DIGEST, now, force=False))

    def test_a_vulnerability_detail_is_read_once_until_osv_modifies_it(self):
        from ..public_registry import vulnerability_reader

        estate()
        inventory("registry.digest", [attested(APP)])
        held = {("GHSA-1", "a", "1"): found("GHSA-1")}
        with (
            mock.patch("hq.platform.application.osv.matches", return_value=(1, {"pkg:npm/a@1": [("GHSA-1", "m")]})),
            mock.patch("hq.platform.application.osv.detail", side_effect=AssertionError("read again")),
        ):
            record = vulnerability_reader(held)(APP)

        self.assertEqual([each["id"] for each in record["findings"]], ["GHSA-1"])
        self.assertEqual(record["unresolved"], [])

    def test_details_past_the_budget_are_left_for_the_next_run(self):
        from ..public_registry import vulnerability_reader

        estate()
        inventory("registry.digest", [attested(APP)])
        with (
            mock.patch(
                "hq.platform.application.osv.matches", return_value=(1, {"pkg:npm/a@1": [("A", "m"), ("B", "m")]})
            ),
            mock.patch("hq.platform.application.osv.detail", return_value={"id": "A"}),
        ):
            record = vulnerability_reader({}, budget=1)(APP)

        self.assertEqual(record["unresolved"], ["B"])


class RegistryReadTests(TestCase):
    def sweep(self):

        with self.captureOnCommitCallbacks(execute=True):
            bridged(
                "inventory",
                {
                    "portainer.container": {
                        "ok": True,
                        "records": list(ProviderInventory.objects.get(kind="portainer.container").records),
                    }
                },
            )

    def test_a_sweep_that_finds_a_digest_hq_has_not_read_starts_the_read(self):
        estate()
        with mock.patch("hq.platform.application.outbound_work.start_for_host") as start:
            self.sweep()
        start.assert_called_once_with("registry.refresh")

    def test_a_sweep_that_finds_nothing_new_is_silent(self):
        from ..public_registry import registry_due

        estate()
        inventory("registry.digest", [attested(APP), attested("docker.io/example/kuma@sha256:bbb")])
        self.assertFalse(registry_due())
        with mock.patch("hq.platform.application.outbound_work.start_for_host") as start:
            self.sweep()
        start.assert_not_called()

    def test_an_image_running_as_another_reference_than_it_was_read_for_is_read_again(self):
        from ..public_registry import registry_due, wanted_images

        estate()
        inventory("registry.digest", [attested(APP), attested("docker.io/example/kuma@sha256:bbb")])
        held = ProviderInventory.objects.get(kind="registry.image")
        read_for = {name: list(references) for name, references in wanted_images().items()}
        held.records = [{**record, "references": read_for.get(record["image"], [])} for record in held.records]
        held.save()
        self.assertFalse(registry_due())

        # An upgrade applied: the container now runs a tag the image was not read for.
        held.records = [
            {**record, "references": ["ghcr.io/example/app:v1.1.0"]}
            if record["image"] == "ghcr.io/example/app"
            else record
            for record in held.records
        ]
        held.save()

        self.assertTrue(registry_due())


class ContainerJoinTests(TestCase):
    def test_every_reading_that_names_a_container_joins_to_it(self):
        from ..relationships import relationships_for

        estate()
        container("web")
        container("app")
        inventory(
            "portainer.network",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "name": "shop_default",
                    "driver": "bridge",
                    "subnets": ["172.20.0.0/16"],
                    "containers": ["web", "app"],
                },
            ],
        )

        found = relationships_for("resource:example-box-web", principal=cli_principal())

        (network,) = found.group("On network").items
        self.assertEqual((network.entity.label, network.entity.detail), ("shop_default", "bridge\xa0· 172.20.0.0/16"))
        self.assertEqual(found.labels("Talks to"), ("app",))

    def test_the_container_page_shows_what_it_does_not_say_elsewhere(self):
        estate()
        container("web")
        inventory(
            "portainer.network",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "name": "shop_default",
                    "driver": "bridge",
                    "containers": ["web"],
                },
            ],
        )
        inventory(
            "portainer.volume",
            [
                {
                    "connection_ref": "example-portainer",
                    "host": "example-box",
                    "type": "bind",
                    "source": "/opt/apps/web/data",
                    "used_by": [{"container": "web", "destination": "/data"}],
                },
            ],
        )
        operator(self.client)

        page = self.client.get(reverse("control_plane:detail", args=["example-box-web"]))

        self.assertContains(page, "On network")
        self.assertContains(page, "shop_default")
        self.assertNotIn("Holds data in", page.context["view"].relationships.phrases())
