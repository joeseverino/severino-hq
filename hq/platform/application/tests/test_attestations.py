from django.test import SimpleTestCase

from ..attestations import CYCLONEDX, SLSA_V02, SLSA_V1, SPDX, github_source, packages_of, provenance_of, reduce


class PackageTests(SimpleTestCase):
    def test_an_spdx_document_names_its_packages_once(self):
        document = {
            "packages": [
                {"externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:npm/express@4.17.1"}]},
                {"externalRefs": [{"referenceType": "cpe23Type", "referenceLocator": "cpe:2.3:a:x"}]},
                {"externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:npm/express@4.17.1"}]},
            ]
        }

        self.assertEqual(packages_of(SPDX, document), ("pkg:npm/express@4.17.1",))

    def test_a_cyclonedx_bom_names_nested_components(self):
        bom = {"components": [{"purl": "pkg:golang/a/b@v1", "components": [{"purl": "pkg:golang/a/c@v2"}]}]}

        self.assertEqual(packages_of(CYCLONEDX, bom), ("pkg:golang/a/b@v1", "pkg:golang/a/c@v2"))

    def test_only_the_first_sbom_and_provenance_are_kept(self):
        found = reduce(
            [
                (
                    SPDX,
                    {"packages": [{"externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:npm/a@1"}]}]},
                ),
                (
                    SLSA_V1,
                    {"runDetails": {"metadata": {"buildkit_metadata": {"vcs": {"source": "https://github.com/o/r"}}}}},
                ),
            ]
        )

        self.assertEqual(
            (found["packages"], found["sbom"], found["provenance"]["source"]),
            (("pkg:npm/a@1",), "SPDX", "https://github.com/o/r"),
        )
        empty = reduce([])
        self.assertEqual((empty["packages"], empty["main"], empty["provenance"]), ((), (), None))

    def test_the_main_module_is_the_go_package_the_others_are_dependencies_of(self):
        from ..attestations import main_modules, module_repository

        def package(spdx_id, purl):
            return {
                "SPDXID": spdx_id,
                "externalRefs": [
                    {"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": purl}
                ],
            }

        main = "pkg:golang/github.com/example/web/v2@v0.0.0-20261118030823-d859bc092c89"
        # Syft's SPDX for a Go binary: each module it embeds, the standard
        # library included, a dependency of the main module; other
        # ecosystems' packages relate among themselves.
        document = {
            "packages": [
                package("SPDXRef-main", main),
                package("SPDXRef-dep", "pkg:golang/github.com/example/dep@v1.2.0"),
                package("SPDXRef-std", "pkg:golang/stdlib@1.26.10"),
                package("SPDXRef-musl", "pkg:apk/alpine/musl@1.2.5-r0"),
                package("SPDXRef-busybox", "pkg:apk/alpine/busybox@1.37.0-r0"),
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-dep",
                    "relatedSpdxElement": "SPDXRef-main",
                    "relationshipType": "DEPENDENCY_OF",
                },
                {
                    "spdxElementId": "SPDXRef-std",
                    "relatedSpdxElement": "SPDXRef-main",
                    "relationshipType": "DEPENDENCY_OF",
                },
                {
                    "spdxElementId": "SPDXRef-musl",
                    "relatedSpdxElement": "SPDXRef-busybox",
                    "relationshipType": "DEPENDENCY_OF",
                },
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relatedSpdxElement": "SPDXRef-main",
                    "relationshipType": "DESCRIBES",
                },
            ],
        }
        bom = {
            "components": [
                {"bom-ref": "m", "purl": main},
                {"bom-ref": "d", "purl": "pkg:golang/github.com/example/dep@v1"},
            ],
            "dependencies": [{"ref": "m", "dependsOn": ["d"]}, {"ref": "d", "dependsOn": []}],
        }

        self.assertEqual(main_modules(SPDX, document), (main,))
        self.assertEqual(main_modules(CYCLONEDX, bom), (main,))
        self.assertEqual(module_repository((main,)), "example/web")
        # Binaries of two repositories name no one source.
        self.assertEqual(module_repository((main, "pkg:golang/github.com/example/other@v1")), "")


class ProvenanceTests(SimpleTestCase):
    def test_buildkit_provenance_names_the_source_and_commit(self):
        found = provenance_of(
            SLSA_V02,
            {
                "builder": {"id": "https://github.com/o/r/actions/runs/1/attempts/1"},
                "materials": [{"uri": "pkg:docker/alpine@3.23?platform=linux%2Famd64"}, {"uri": "pkg:npm/x@1"}],
                "metadata": {
                    "buildFinishedOn": "2026-09-20T00:00:00Z",
                    "https://mobyproject.org/buildkit@v1#metadata": {
                        "vcs": {"source": "git@github.com:o/r.git", "revision": "abc123"}
                    },
                },
            },
        )

        self.assertEqual(found["source"], "git@github.com:o/r.git")
        self.assertEqual(found["revision"], "abc123")
        self.assertEqual(found["builder"], "https://github.com/o/r/actions/runs/1/attempts/1")
        self.assertEqual(found["materials"], ("pkg:docker/alpine@3.23?platform=linux%2Famd64",))

    def test_the_workflows_repository_is_where_it_was_built_not_what_from(self):
        found = provenance_of(
            SLSA_V02,
            {
                "invocation": {"environment": {"github_repository": "o/release", "github_run_id": "9"}},
                "metadata": {},
            },
        )

        self.assertEqual(found["source"], "")
        self.assertEqual(found["builder"], "https://github.com/o/release/actions/runs/9")

    def test_slsa_one_reads_buildkits_metadata(self):
        found = provenance_of(
            SLSA_V1,
            {
                "buildDefinition": {"resolvedDependencies": [{"uri": "pkg:docker/node@22"}]},
                "runDetails": {
                    "metadata": {
                        "finishedOn": "2026-09-20T00:00:00Z",
                        "buildkit_metadata": {"vcs": {"source": "https://github.com/o/r.git", "revision": "def"}},
                    }
                },
            },
        )

        self.assertEqual(
            (found["format"], found["source"], found["materials"]),
            ("SLSA 1.0", "https://github.com/o/r.git", ("pkg:docker/node@22",)),
        )

    def test_a_github_source_in_every_form_a_build_reports_it(self):
        self.assertEqual(github_source("git@github.com:example/monitor.git"), "example/monitor")
        self.assertEqual(github_source("https://github.com/example/proxy.git"), "example/proxy")
        self.assertEqual(github_source("https://github.com/example/identity"), "example/identity")
        # A private forge is a source, but not one HQ can read releases from.
        self.assertEqual(github_source("ssh://git.example.test:7999/dns/app.git"), "")
        self.assertEqual(github_source(""), "")
