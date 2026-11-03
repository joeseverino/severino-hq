from contextlib import contextmanager

from django.test import SimpleTestCase

from ..images import ImageRef
from ..oci_registry import MAX_RESPONSE_BYTES, RegistryReadError, digest_of, labels, tags
from .network import PUBLIC, Response, answering, moved, resolving

# The made-up hosts a registry, its token service and its CDN answer at.
PUBLIC_NAMES = {"registry.example": PUBLIC, "auth.example": PUBLIC, "cdn.example": PUBLIC}


def _challenge(realm="https://auth.example/token"):
    return Response(
        {"errors": [{"code": "UNAUTHORIZED"}]},
        {"WWW-Authenticate": f'Bearer realm="{realm}",service="registry.example"'},
        status=401,
    )


class FakeRegistry:
    """A public registry: anonymous token first, then pages of tags."""

    def __init__(self, *, refuse_token=False):
        self.refuse_token = refuse_token
        self.calls = []

    def __call__(self, request):
        url = str(request.url)
        self.calls.append(request.url)
        authorized = request.headers.get("Authorization") == "Bearer anonymous"
        if url.startswith("https://auth.example/token"):
            if self.refuse_token:
                return Response({"errors": [{"code": "UNAUTHORIZED"}]}, status=401)
            return Response({"token": "anonymous"})
        if not authorized:
            return _challenge()
        if "/tags/list" in url and "last=" not in url:
            return Response(
                {"tags": ["1.0.0", "1.1.0"]}, {"Link": '</v2/team/app/tags/list?last=1.1.0&n=1000>; rel="next"'}
            )
        if "/tags/list" in url:
            return Response({"tags": ["2.0.0"]})
        if "/manifests/" in url and "sha256:platform" not in url:
            return Response(
                {
                    "manifests": [
                        {"digest": "sha256:other", "platform": {"os": "linux", "architecture": "arm64"}},
                        {"digest": "sha256:platform", "platform": {"os": "linux", "architecture": "amd64"}},
                    ]
                }
            )
        if "/manifests/sha256:platform" in url:
            return Response({"config": {"digest": "sha256:config"}})
        if "/blobs/sha256:config" in url:
            return Response({"config": {"Labels": {"org.opencontainers.image.source": "https://github.com/team/app"}}})
        raise AssertionError(url)


@contextmanager
def reads(fake):
    """The fake registry answering, at made-up hosts that resolve to the internet."""

    with resolving(PUBLIC_NAMES), answering(fake):
        yield


class RegistryTests(SimpleTestCase):
    image = ImageRef.parse("registry.example/team/app:1.0.0")

    def test_tags_are_read_anonymously_across_pages(self):
        fake = FakeRegistry()
        with reads(fake):
            found = tags(self.image)

        self.assertEqual(found, ["1.0.0", "1.1.0", "2.0.0"])
        token_url = next(call for call in fake.calls if call.host == "auth.example")
        self.assertEqual(token_url.params["scope"], "repository:team/app:pull")

    def test_labels_come_from_the_platform_the_machines_run(self):
        with reads(FakeRegistry()):
            found = labels(self.image)

        self.assertEqual(found["org.opencontainers.image.source"], "https://github.com/team/app")

    def test_a_tag_resolves_to_its_digest_with_a_head_request(self):
        digest = "sha256:" + "f" * 64
        seen = []

        def head(request):
            seen.append(request.method)
            if request.headers.get("Authorization") != "Bearer anonymous":
                if request.url.host == "auth.example":
                    return Response({"token": "anonymous"})
                return _challenge()
            return Response({}, {"Docker-Content-Digest": digest})

        with reads(head):
            self.assertEqual(digest_of(self.image, "1.0.0"), digest)
        self.assertIn("HEAD", seen)

    def test_a_private_image_says_so(self):
        with (
            reads(FakeRegistry(refuse_token=True)),
            self.assertRaisesMessage(RegistryReadError, "may be private"),
        ):
            tags(self.image)


class BoundaryTests(SimpleTestCase):
    """Every host here is named by someone else, so none is trusted."""

    def test_a_registry_on_a_private_address_is_never_asked(self):
        def opened(request):
            raise AssertionError("no request may be made")

        with resolving({}), answering(opened):
            for reference in ("localhost:5000/team/app:1", "127.0.0.1:5000/team/app:1", "10.0.0.5:5000/team/app:1"):
                with self.assertRaisesMessage(RegistryReadError, "not a public address"):
                    tags(ImageRef.parse(reference))

    def test_a_token_service_the_registry_names_is_checked_too(self):
        def challenge_to_internal(request):
            return _challenge("https://internal.example/token")

        with (
            resolving({"registry.example": PUBLIC, "internal.example": "10.0.0.7"}),
            answering(challenge_to_internal),
            self.assertRaisesMessage(RegistryReadError, "internal.example is not a public address"),
        ):
            tags(ImageRef.parse("registry.example/team/app:1"))

    def blob_moved_to(self, location):
        """The blob a registry moves to ``location``, and every request made for it."""

        asked = []

        def answer(request):
            asked.append((str(request.url), request.headers.get("Authorization")))
            if request.url.host == "auth.example":
                return Response({"token": "anonymous"})
            if request.url.host == "registry.example":
                if request.headers.get("Authorization") != "Bearer anonymous":
                    return _challenge()
                if "/blobs/" in request.url.path:
                    return moved(location, 307)
                return Response({"config": {"digest": "sha256:config"}})
            return Response({"config": {"Labels": {"example": "label"}}})

        with resolving({**PUBLIC_NAMES, "inside.example": "10.0.0.5"}), answering(answer):
            try:
                found = labels(ImageRef.parse("registry.example/team/app@sha256:platform"))
            except RegistryReadError as exc:
                found = exc
        return found, asked

    def test_a_redirect_to_a_public_cdn_is_followed_without_the_registrys_token(self):
        found, asked = self.blob_moved_to("https://cdn.example/blob")

        self.assertEqual(found, {"example": "label"})
        self.assertEqual(asked[-1], ("https://cdn.example/blob", None))

    def test_a_redirect_to_plain_http_or_inside_is_never_asked(self):
        for location in ("http://cdn.example/blob", "https://inside.example/blob", "https://10.0.0.5/blob"):
            with self.subTest(location=location):
                found, asked = self.blob_moved_to(location)

                self.assertIsInstance(found, RegistryReadError)
                # The blob was asked for, and the place it moved to never was.
                self.assertEqual(asked[-1][0], "https://registry.example/v2/team/app/blobs/sha256:config")

    def test_an_oversized_answer_is_refused(self):
        with (
            reads(lambda request: Response(content=b"x" * (MAX_RESPONSE_BYTES + 1))),
            self.assertRaisesMessage(RegistryReadError, "more than"),
        ):
            tags(ImageRef.parse("registry.example/team/app:1"))


class AttestationTests(SimpleTestCase):
    image = ImageRef.parse("registry.example/team/app@sha256:index")

    def registry(self, layers):
        blobs = {f"sha256:{name}": body for name, (_type, _size, body) in layers.items()}

        def answer(request):
            url = str(request.url)
            if url.startswith("https://auth.example/token"):
                return Response({"token": "anonymous"})
            if request.headers.get("Authorization") != "Bearer anonymous":
                return _challenge()
            if url.endswith("/manifests/sha256:index"):
                return Response(
                    {
                        "manifests": [
                            {"digest": "sha256:platform", "platform": {"os": "linux", "architecture": "amd64"}},
                            {
                                "digest": "sha256:attached",
                                "platform": {"os": "unknown", "architecture": "unknown"},
                                "annotations": {
                                    "vnd.docker.reference.type": "attestation-manifest",
                                    "vnd.docker.reference.digest": "sha256:platform",
                                },
                            },
                        ]
                    }
                )
            if url.endswith("/manifests/sha256:attached"):
                return Response(
                    {
                        "layers": [
                            {
                                "digest": f"sha256:{name}",
                                "size": size,
                                "annotations": {"in-toto.io/predicate-type": kind},
                            }
                            for name, (kind, size, _body) in layers.items()
                        ]
                    }
                )
            for digest, body in blobs.items():
                if url.endswith(f"/blobs/{digest}"):
                    return Response(body)
            raise AssertionError(url)

        return reads(answer)

    def test_the_sbom_and_provenance_beside_the_platform_are_read(self):
        from ..oci_registry import attestations

        with self.registry(
            {
                "sbom": ("https://spdx.dev/Document", 10, {"predicate": {"packages": []}}),
                "provenance": ("https://slsa.dev/provenance/v1", 10, {"predicate": {"runDetails": {}}}),
                "other": ("https://example.test/unknown", 10, {"predicate": {}}),
            }
        ):
            found = attestations(self.image, "sha256:index")

        self.assertEqual(found["platform_digest"], "sha256:platform")
        self.assertEqual(
            [kind for kind, _predicate in found["statements"]],
            ["https://spdx.dev/Document", "https://slsa.dev/provenance/v1"],
        )

    def test_an_attestation_too_large_to_be_one_is_refused_before_it_is_fetched(self):
        from ..oci_registry import MAX_STATEMENT_BYTES, attestations

        with (
            self.registry({"sbom": ("https://spdx.dev/Document", MAX_STATEMENT_BYTES + 1, {})}),
            self.assertRaisesMessage(RegistryReadError, "larger than HQ reads"),
        ):
            attestations(self.image, "sha256:index")

    def test_an_image_with_nothing_attached_has_no_statements(self):
        from ..oci_registry import attestations

        def answer(request):
            if str(request.url).startswith("https://auth.example/token"):
                return Response({"token": "anonymous"})
            if request.headers.get("Authorization") != "Bearer anonymous":
                return _challenge()
            return Response(
                {"manifests": [{"digest": "sha256:platform", "platform": {"os": "linux", "architecture": "amd64"}}]}
            )

        with reads(answer):
            self.assertEqual(
                attestations(self.image, "sha256:index"), {"platform_digest": "sha256:platform", "statements": []}
            )
