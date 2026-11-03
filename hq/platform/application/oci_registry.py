"""Reading an image's tags, labels and attestations from its registry, anonymously.

The OCI distribution API is the same on Docker Hub, GitHub's registry and Quay,
and a public repository hands out an anonymous pull token, so no credential
exists to hold and HQ reads it itself (``application.public_registry``) rather
than a controller. Every host here is named by someone else, so each request is
checked before it is made (docs/Derived-Facts.md#containers-and-images).
"""

import json
import re
import urllib.parse
from typing import Any

import httpx2

from .http_read import UNREACHABLE, fetch
from .images import DOCKER_HUB, ImageRef
from .reach import public_host

TIMEOUT_SECONDS = 10
# Docker Hub serves its registry API from a different host than its name.
_API_HOST = {DOCKER_HUB: "registry-1.docker.io"}
PAGE_SIZE = 1000
# Enough for any repository's recent history; nginx alone has thousands.
MAX_PAGES = 5
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')
_NEXT = re.compile(r"<([^>]+)>;\s*rel=\"?next\"?")


# A tag list or a manifest is kilobytes; anything near this is not one.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class RegistryReadError(Exception):
    """The registry could not be read, in words a person can act on."""


class RegistryPrivate(RegistryReadError):
    """The registry will not hand an anonymous reader this image: an answer
    about the image, not an outage, so it is kept as the image's reading
    rather than retried as a failure."""


# Named here so a test can stand in for resolution at the one place it is asked.
_public_host = public_host


def _checked(request: httpx2.Request) -> None:
    """Every request, the first and each redirect to a registry's CDN, checked
    before it is made. httpx2 leaves the registry's token behind once a
    redirect changes origin."""

    if request.url.scheme != "https" or not request.url.host:
        raise RegistryReadError("A registry pointed somewhere other than an HTTPS address.")
    if not _public_host(request.url.host):
        raise RegistryReadError(f"{request.url.host} is not a public address, so HQ does not read it.")


def tags(image: ImageRef) -> list[str]:
    """Every tag the registry lists for ``image``'s repository."""

    host = _API_HOST.get(image.registry, image.registry)
    path = f"/v2/{image.repository}/tags/list?n={PAGE_SIZE}"
    token = ""
    found: list[str] = []
    for _page in range(MAX_PAGES):
        body, headers, token = _get(host, path, image, token)
        found.extend(str(tag) for tag in (body.get("tags") or ()) if tag)
        following = _NEXT.search(headers.get("Link", "") or "")
        if not following:
            break
        path = following.group(1)
        # The next page is on the same registry, whatever host the link names.
        path = urllib.parse.urlsplit(path)._replace(scheme="", netloc="").geturl()
    return found


# A multi-platform image is an index of per-platform manifests; either answers.
_MANIFESTS = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
PLATFORM = ("linux", "amd64")


def _platform(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """The linux/amd64 entry of an index, or its first."""

    return next(
        (
            item
            for item in manifests
            if (item.get("platform") or {}).get("os") == PLATFORM[0]
            and (item.get("platform") or {}).get("architecture") == PLATFORM[1]
        ),
        manifests[0],
    )


def labels(image: ImageRef) -> dict[str, str]:
    """The labels the image was built with, for the digest or tag it names.

    From the image's own config, so the repository it says it is built from
    is the image's word, not a guess from its name.
    """

    host = _API_HOST.get(image.registry, image.registry)
    reference = image.digest or image.tag or "latest"
    manifest, _headers, token = _get(host, f"/v2/{image.repository}/manifests/{reference}", image, "", _MANIFESTS)
    if manifest.get("manifests"):
        chosen = _platform(manifest["manifests"])
        manifest, _headers, token = _get(
            host, f"/v2/{image.repository}/manifests/{chosen.get('digest', '')}", image, token, _MANIFESTS
        )
    config = (manifest.get("config") or {}).get("digest", "")
    if not config:
        return {}
    body, _headers, _token = _get(host, f"/v2/{image.repository}/blobs/{config}", image, token)
    found = (body.get("config") or {}).get("Labels") or {}
    return {str(key): str(value) for key, value in found.items()}


# Where BuildKit attaches an image's SBOM and provenance: a manifest beside the
# platform's in the index, one in-toto statement per layer.
_ATTESTATION = "attestation-manifest"
SBOM_PREDICATES = ("https://spdx.dev/Document", "https://cyclonedx.org/bom")
PROVENANCE_PREDICATES = ("https://slsa.dev/provenance/v0.2", "https://slsa.dev/provenance/v1")
# An SBOM names every package in the image; a large one is a few megabytes.
MAX_STATEMENT_BYTES = 32 * 1024 * 1024


def attestations(image: ImageRef, digest: str) -> dict[str, Any]:
    """What the publisher attached to ``digest``: ``{platform_digest,
    statements: [(predicate type, predicate)]}``, SBOM and provenance only.

    Metadata beside the image, read like its manifest: no layer of the image
    itself is fetched, and nothing is run.
    """

    host = _API_HOST.get(image.registry, image.registry)
    index, _headers, token = _get(host, f"/v2/{image.repository}/manifests/{digest}", image, "", _MANIFESTS)
    manifests = [item for item in index.get("manifests") or () if isinstance(item, dict)]
    runnable = [
        item for item in manifests if (item.get("annotations") or {}).get("vnd.docker.reference.type") != _ATTESTATION
    ]
    if not runnable:
        return {"platform_digest": digest, "statements": []}
    platform = str(_platform(runnable).get("digest", ""))
    attached = next(
        (
            item
            for item in manifests
            if (item.get("annotations") or {}).get("vnd.docker.reference.type") == _ATTESTATION
            and (item.get("annotations") or {}).get("vnd.docker.reference.digest") == platform
        ),
        None,
    )
    if attached is None:
        return {"platform_digest": platform, "statements": []}
    manifest, _headers, token = _get(
        host, f"/v2/{image.repository}/manifests/{attached.get('digest', '')}", image, token, _MANIFESTS
    )
    statements = []
    for layer in manifest.get("layers") or ():
        predicate = str((layer.get("annotations") or {}).get("in-toto.io/predicate-type", ""))
        if predicate not in SBOM_PREDICATES + PROVENANCE_PREDICATES:
            continue
        if int(layer.get("size") or 0) > MAX_STATEMENT_BYTES:
            raise RegistryReadError(f"{image.name}: an attestation is larger than HQ reads.")
        body, _headers, token = _get(
            host, f"/v2/{image.repository}/blobs/{layer.get('digest', '')}", image, token, limit=MAX_STATEMENT_BYTES
        )
        statements.append((predicate, body.get("predicate") or {}))
    return {"platform_digest": platform, "statements": statements}


def digest_of(image: ImageRef, tag: str) -> str:
    """The digest ``tag`` names now (``sha256:…``), from a ``HEAD`` of its manifest.

    ``HEAD`` because Docker Hub counts a manifest ``GET`` against its anonymous
    pull limit and does not count this.
    """

    host = _API_HOST.get(image.registry, image.registry)
    _body, headers, _token = _get(host, f"/v2/{image.repository}/manifests/{tag}", image, "", _MANIFESTS, method="HEAD")
    found = str(headers.get("Docker-Content-Digest", "") or "")
    return found if re.fullmatch(r"sha256:[0-9a-f]{64}", found) else ""


def _get(
    host: str,
    path: str,
    image: ImageRef,
    token: str,
    accept: str = "application/json",
    method: str = "GET",
    limit: int = MAX_RESPONSE_BYTES,
) -> tuple[dict[str, Any], httpx2.Headers, str]:
    response, body = _request(f"https://{host}{path}", token, accept, method, limit)
    challenge = response.headers.get("WWW-Authenticate", "") or ""
    if response.status_code == 401 and not token and challenge.lower().startswith("bearer"):
        token = _anonymous_token(challenge, image)
        response, body = _request(f"https://{host}{path}", token, accept, method, limit)
    if not response.is_success:
        raise RegistryReadError(f"{image.name}: the registry returned HTTP {response.status_code}.")
    return ({} if method == "HEAD" else _json(body, limit)), response.headers, token


def _request(
    url: str, token: str, accept: str = "application/json", method: str = "GET", limit: int = MAX_RESPONSE_BYTES
) -> tuple[httpx2.Response, bytes]:
    headers = {"Accept": accept, "User-Agent": "Severino-HQ"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return fetch(
            method,
            url,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            limit=limit,
            follow_redirects=True,
            on_request=(_checked,),
        )
    except UNREACHABLE as exc:
        raise RegistryReadError(f"Could not reach the registry: {exc}") from exc


def _json(body: bytes, limit: int) -> dict[str, Any]:
    if len(body) > limit:
        raise RegistryReadError("The registry answered with more than HQ reads for this.")
    try:
        found = json.loads(body)
    except ValueError as exc:
        raise RegistryReadError("The registry answered with something other than JSON.") from exc
    if not isinstance(found, dict):
        raise RegistryReadError("The registry answered with something other than an object.")
    return found


def _anonymous_token(challenge: str, image: ImageRef) -> str:
    fields = dict(_CHALLENGE.findall(challenge))
    realm = fields.get("realm", "")
    if not realm.startswith("https://"):
        raise RegistryReadError(f"{image.name}: the registry offers no anonymous access.")
    query = {"service": fields.get("service", ""), "scope": f"repository:{image.repository}:pull"}
    url = f"{realm}?{urllib.parse.urlencode({key: value for key, value in query.items() if value})}"
    response, raw = _request(url, "")
    if not response.is_success:
        raise RegistryPrivate(
            f"{image.name}: the registry refused an anonymous read (HTTP {response.status_code}); "
            "the image may be private."
        )
    body = _json(raw, MAX_RESPONSE_BYTES)
    token = str(body.get("token") or body.get("access_token") or "")
    if not token:
        raise RegistryReadError(f"{image.name}: the registry gave no anonymous token.")
    return token
