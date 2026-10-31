"""What HQ reads for itself: the images its containers run, and the releases
and advisories GitHub publishes for the repository an image is built from."""

import io
from email.message import Message
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.domains.control_plane.dns_lookup import LookupNotFound, LookupUnavailable
from hq.domains.control_plane.models import ProviderInventory

from ..public_registry import read_upstream, wanted_images
from .network import Response, answering


def inventory(kind, records):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={"records": records, "reachable": True, "connected": True, "observed_at": timezone.now()},
    )


def container(name, image, host="example-box"):
    return {"name": name, "image": image, "state": "running", "host": host}


def pulled(tags, *users, host="example-box"):
    return {
        "host": host,
        "id": "sha256:" + "a" * 64,
        "tags": list(tags),
        "containers": [{"container": user} for user in users],
    }


class WantedImagesTests(TestCase):
    def test_each_image_running_is_wanted_by_the_references_it_runs_as(self):
        inventory(
            "portainer.container",
            [
                container("web", "example/web:1.0.0"),
                container("worker", "example/web:1.1.0"),
                container("hub", "ghcr.io/example/hub:2"),
                container("broken", ""),
            ],
        )

        self.assertEqual(
            wanted_images(),
            {
                "docker.io/example/web": ("example/web:1.0.0", "example/web:1.1.0"),
                "ghcr.io/example/hub": ("ghcr.io/example/hub:2",),
            },
        )

    def test_an_image_pinned_by_digest_alone_is_also_wanted_by_the_tag_it_was_pulled_as(self):
        digest = "example/web@sha256:" + "b" * 64
        inventory("portainer.container", [container("web", digest), container("other", digest, host="example-other")])
        inventory(
            "portainer.image",
            [
                pulled(("example/web:1.0.0", "example/web:latest", "example/other:3"), "web"),
                pulled(("example/web:9.9.9",), "web", host="example-elsewhere"),
            ],
        )

        self.assertEqual(
            wanted_images(),
            {"docker.io/example/web": tuple(sorted((digest, "example/web:1.0.0", "example/web:latest")))},
        )


RELEASES = [
    {
        "tag_name": "v1.2.0",
        "html_url": "https://github.com/example/app/releases/tag/v1.2.0",
        "published_at": "2026-01-02T00:00:00Z",
        "prerelease": False,
        "draft": False,
    },
    {
        "tag_name": "v1.3.0-rc.1",
        "html_url": "https://github.com/example/app/releases/tag/v1.3.0-rc.1",
        "prerelease": True,
        "draft": False,
    },
    {"tag_name": "v1.3.0", "html_url": "", "prerelease": False, "draft": True},
    {"tag_name": "", "prerelease": False, "draft": False},
    {
        "tag_name": "v1.1.0",
        "html_url": "https://example.com/elsewhere",
        "published_at": "2025-12-01T00:00:00Z",
        "prerelease": False,
        "draft": False,
    },
]
ADVISORIES = [
    {
        "ghsa_id": "GHSA-aaaa-bbbb-cccc",
        "cve_id": "CVE-2026-0001",
        "severity": "high",
        "summary": "A flaw.",
        "html_url": "https://github.com/example/app/security/advisories/GHSA-aaaa-bbbb-cccc",
        "published_at": "2026-01-03T00:00:00Z",
        "vulnerabilities": [
            {
                "package": {"ecosystem": "go", "name": "example"},
                "vulnerable_version_range": "< 1.2.0",
                "patched_versions": "1.2.0",
            },
            "not an entry",
        ],
    },
    {"ghsa_id": "GHSA-dddd-eeee-ffff", "cve_id": None, "severity": "low", "summary": "", "vulnerabilities": None},
    "not an advisory",
]


def github(releases, advisories):
    """GitHub's API, answering each page with its body, or 404 for None."""

    def answer(request, _timeout):
        body = releases if "/releases" in request.full_url else advisories
        if body is None:
            raise HTTPError(request.full_url, 404, "Not Found", Message(), io.BytesIO())
        return Response(body)

    return answering(answer)


class ReadUpstreamTests(SimpleTestCase):
    def test_it_keeps_published_releases_and_every_advisory_with_its_ranges(self):
        with github(RELEASES, ADVISORIES):
            read = read_upstream("example/app")

        self.assertEqual(read["url"], "https://github.com/example/app")
        self.assertEqual([release["tag"] for release in read["releases"]], ["v1.2.0", "v1.1.0"])
        self.assertEqual(read["releases"][0]["url"], "https://github.com/example/app/releases/tag/v1.2.0")
        # A link only becomes an href when it is a GitHub page.
        self.assertEqual(read["releases"][1]["url"], "")
        first, second = read["advisories"]
        self.assertEqual((first["id"], first["severity"]), ("CVE-2026-0001", "high"))
        self.assertEqual(first["vulnerabilities"], [["< 1.2.0", "1.2.0"]])
        self.assertEqual((second["id"], second["vulnerabilities"], second["url"]), ("GHSA-dddd-eeee-ffff", [], ""))

    def test_a_repository_with_only_one_page_is_still_read(self):
        with github(None, ADVISORIES):
            read = read_upstream("example/app")

        self.assertEqual((read["releases"], len(read["advisories"])), ([], 2))

    def test_a_repository_github_does_not_have_is_not_found(self):
        with github(None, None), self.assertRaises(LookupNotFound):
            read_upstream("example/gone")

    def test_github_out_of_reach_is_unavailable(self):
        def unreachable(request, _timeout):
            raise URLError("connection refused")

        with answering(unreachable), self.assertRaises(LookupUnavailable):
            read_upstream("example/app")
