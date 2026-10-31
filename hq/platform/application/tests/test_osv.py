import json
from urllib.error import URLError

from django.test import SimpleTestCase

from ..osv import OSVReadError, finding, matches, query_of
from .network import Response, answering


class QueryTests(SimpleTestCase):
    def test_distribution_packages_are_asked_by_release(self):
        self.assertEqual(
            query_of("pkg:apk/alpine/busybox@1.36.1-r0?os_name=alpine&os_version=3.19.4"),
            {"package": {"ecosystem": "Alpine:v3.19", "name": "busybox"}, "version": "1.36.1-r0"},
        )
        self.assertEqual(
            query_of("pkg:deb/debian/libssl3@3.0.11-1~deb12u1?distro=debian-12&upstream=openssl%403.0.11"),
            {"package": {"ecosystem": "Debian:12", "name": "openssl"}, "version": "3.0.11-1~deb12u1"},
        )

    def test_language_packages_are_asked_by_their_url_without_qualifiers(self):
        self.assertEqual(
            query_of("pkg:golang/github.com/docker/docker@v28.5.1%2Bincompatible?type=module"),
            {"package": {"purl": "pkg:golang/github.com/docker/docker@v28.5.1%2Bincompatible"}},
        )
        self.assertEqual(query_of("pkg:npm/%40scope/name@1.0.0"), {"package": {"purl": "pkg:npm/%40scope/name@1.0.0"}})

    def test_what_osv_cannot_match_is_not_asked(self):
        self.assertIsNone(query_of("pkg:rpm/redhat/openssl@3.0"))
        self.assertIsNone(query_of("pkg:apk/alpine/busybox@1.36.1-r0"))  # no release named
        self.assertIsNone(query_of("not a purl"))


class MatchTests(SimpleTestCase):
    def test_every_matched_package_and_how_many_were_asked(self):
        asked = []

        def osv(request, timeout=None):
            asked.append(request)
            # The shape of OSV's querybatch answer: one result per query, in order.
            return Response({"results": [{"vulns": [{"id": "GHSA-1", "modified": "m"}]}, {}]})

        with answering(osv):
            checked, found = matches(["pkg:npm/a@1", "pkg:npm/b@2", "pkg:rpm/x/y@1"])

        self.assertEqual((checked, found), (2, {"pkg:npm/a@1": [("GHSA-1", "m")]}))
        (request,) = asked
        self.assertEqual((request.get_method(), request.full_url), ("POST", "https://api.osv.dev/v1/querybatch"))
        self.assertEqual(len(json.loads(request.data)["queries"]), 2)

    def test_an_unreachable_osv_says_so(self):
        def unreachable(request, timeout=None):
            raise URLError("no route to host")

        with answering(unreachable), self.assertRaisesMessage(OSVReadError, "Could not reach OSV"):
            matches(["pkg:npm/a@1"])


class FindingTests(SimpleTestCase):
    def test_a_finding_names_what_fixes_it_for_that_package(self):
        vulnerability = {
            "id": "GHSA-x",
            "summary": "bad",
            "aliases": ["CVE-1"],
            "database_specific": {"severity": "HIGH"},
            "affected": [
                {
                    "package": {"name": "github.com/docker/cli"},
                    "ranges": [{"events": [{"introduced": "0"}, {"fixed": "29.2.0"}]}],
                },
                {"package": {"name": "github.com/other/thing"}, "ranges": [{"events": [{"fixed": "9.9.9"}]}]},
            ],
        }

        found = finding(vulnerability, "pkg:golang/github.com/docker/cli@v28.5.1", "m")

        self.assertEqual(found["package"], "github.com/docker/cli")
        self.assertEqual((found["installed"], found["fixed"], found["severity"]), ("v28.5.1", ("29.2.0",), "high"))
        self.assertEqual(found["url"], "https://osv.dev/vulnerability/GHSA-x")

    def test_an_unrated_distribution_finding_stays_unrated(self):
        found = finding(
            {"id": "CVE-2", "affected": [{"package": {"name": "busybox"}, "ranges": []}]},
            "pkg:apk/alpine/busybox@1.36.1-r0?os_version=3.19",
        )

        self.assertEqual((found["package"], found["severity"], found["fixed"]), ("busybox", "", ()))
