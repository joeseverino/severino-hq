import io
import json
import urllib.parse
from email.message import Message
from unittest import mock
from urllib.error import HTTPError

from django.test import SimpleTestCase

from ..images import ImageRef
from ..oci_registry import MAX_RESPONSE_BYTES, RegistryReadError, _CheckedRedirects, digest_of, labels, tags


class _Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _challenge(url):
    headers = Message()
    headers["WWW-Authenticate"] = 'Bearer realm="https://auth.example/token",service="registry.example"'
    return HTTPError(url, 401, "Unauthorized", headers, io.BytesIO())


class FakeRegistry:
    """A public registry: anonymous token first, then pages of tags."""

    def __init__(self, *, refuse_token=False):
        self.refuse_token = refuse_token
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        authorized = request.headers.get("Authorization") == "Bearer anonymous"
        if url.startswith("https://auth.example/token"):
            if self.refuse_token:
                raise HTTPError(url, 401, "Unauthorized", Message(), io.BytesIO())
            return _Response({"token": "anonymous"})
        if not authorized:
            raise _challenge(url)
        if "/tags/list" in url and "last=" not in url:
            return _Response(
                {"tags": ["1.0.0", "1.1.0"]}, {"Link": '</v2/team/app/tags/list?last=1.1.0&n=1000>; rel="next"'}
            )
        if "/tags/list" in url:
            return _Response({"tags": ["2.0.0"]})
        if "/manifests/" in url and "sha256:platform" not in url:
            return _Response(
                {
                    "manifests": [
                        {"digest": "sha256:other", "platform": {"os": "linux", "architecture": "arm64"}},
                        {"digest": "sha256:platform", "platform": {"os": "linux", "architecture": "amd64"}},
                    ]
                }
            )
        if "/manifests/sha256:platform" in url:
            return _Response({"config": {"digest": "sha256:config"}})
        if "/blobs/sha256:config" in url:
            return _Response({"config": {"Labels": {"org.opencontainers.image.source": "https://github.com/team/app"}}})
        raise AssertionError(url)


def reads(fake):
    """The fake registry answering, with its made-up hosts treated as public."""

    return mock.patch.multiple(
        "hq.platform.application.oci_registry",
        _public_host=mock.Mock(return_value=True),
        _opener=mock.Mock(open=fake),
    )


class RegistryTests(SimpleTestCase):
    image = ImageRef.parse("registry.example/team/app:1.0.0")

    def test_tags_are_read_anonymously_across_pages(self):
        fake = FakeRegistry()
        with reads(fake):
            found = tags(self.image)

        self.assertEqual(found, ["1.0.0", "1.1.0", "2.0.0"])
        token_url = next(call for call in fake.calls if urllib.parse.urlsplit(call).hostname == "auth.example")
        self.assertIn("scope=repository%3Ateam%2Fapp%3Apull", token_url)

    def test_labels_come_from_the_platform_the_machines_run(self):
        with reads(FakeRegistry()):
            found = labels(self.image)

        self.assertEqual(found["org.opencontainers.image.source"], "https://github.com/team/app")

    def test_a_tag_resolves_to_its_digest_with_a_head_request(self):
        digest = "sha256:" + "f" * 64
        seen = []

        def head(request, timeout=None):
            seen.append(request.get_method())
            if request.headers.get("Authorization") != "Bearer anonymous":
                if urllib.parse.urlsplit(request.full_url).hostname == "auth.example":
                    return _Response({"token": "anonymous"})
                raise _challenge(request.full_url)
            return _Response({}, {"Docker-Content-Digest": digest})

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
        opened = mock.Mock(side_effect=AssertionError("no request may be made"))
        with mock.patch("hq.platform.application.oci_registry._opener", mock.Mock(open=opened)):
            for reference in ("localhost:5000/team/app:1", "127.0.0.1:5000/team/app:1", "10.0.0.5:5000/team/app:1"):
                with self.assertRaisesMessage(RegistryReadError, "not a public address"):
                    tags(ImageRef.parse(reference))

    def test_a_token_service_the_registry_names_is_checked_too(self):
        def public(host):
            return host != "internal.example"

        def challenge_to_internal(request, timeout=None):
            headers = Message()
            headers["WWW-Authenticate"] = 'Bearer realm="https://internal.example/token",service="x"'
            raise HTTPError(request.full_url, 401, "Unauthorized", headers, io.BytesIO())

        with (
            mock.patch.multiple(
                "hq.platform.application.oci_registry",
                _public_host=mock.Mock(side_effect=public),
                _opener=mock.Mock(open=challenge_to_internal),
            ),
            self.assertRaisesMessage(RegistryReadError, "internal.example is not a public address"),
        ):
            tags(ImageRef.parse("registry.example/team/app:1"))

    def test_a_redirect_is_checked_and_leaves_the_token_behind(self):
        from urllib.request import Request

        handler = _CheckedRedirects()
        request = Request(
            "https://registry.example/v2/team/app/blobs/sha256:x", headers={"Authorization": "Bearer anonymous"}
        )
        with mock.patch("hq.platform.application.oci_registry._public_host", return_value=True):
            followed = handler.redirect_request(
                request, None, 307, "Temporary Redirect", Message(), "https://cdn.example/blob"
            )
        self.assertIsNone(followed.get_header("Authorization"))
        with (
            mock.patch("hq.platform.application.oci_registry._public_host", return_value=False),
            self.assertRaises(RegistryReadError),
        ):
            handler.redirect_request(request, None, 307, "Temporary Redirect", Message(), "https://10.0.0.5/blob")
        with self.assertRaises(RegistryReadError):
            handler.redirect_request(request, None, 307, "Temporary Redirect", Message(), "http://cdn.example/blob")

    def test_an_oversized_answer_is_refused(self):
        class Huge(_Response):
            def read(self, size=-1):
                return b"x" * (MAX_RESPONSE_BYTES + 1)

        with (
            reads(lambda request, timeout=None: Huge({})),
            self.assertRaisesMessage(RegistryReadError, "more than"),
        ):
            tags(ImageRef.parse("registry.example/team/app:1"))


class AttestationTests(SimpleTestCase):
    image = ImageRef.parse("registry.example/team/app@sha256:index")

    def registry(self, layers):
        blobs = {f"sha256:{name}": body for name, (_type, _size, body) in layers.items()}

        def answer(request, timeout=None):
            url = request.full_url
            if url.startswith("https://auth.example/token"):
                return _Response({"token": "anonymous"})
            if request.headers.get("Authorization") != "Bearer anonymous":
                raise _challenge(url)
            if url.endswith("/manifests/sha256:index"):
                return _Response(
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
                return _Response(
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
                    return _Response(body)
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

        def answer(request, timeout=None):
            if request.full_url.startswith("https://auth.example/token"):
                return _Response({"token": "anonymous"})
            if request.headers.get("Authorization") != "Bearer anonymous":
                raise _challenge(request.full_url)
            return _Response(
                {"manifests": [{"digest": "sha256:platform", "platform": {"os": "linux", "architecture": "amd64"}}]}
            )

        with reads(answer):
            self.assertEqual(
                attestations(self.image, "sha256:index"), {"platform_digest": "sha256:platform", "statements": []}
            )
