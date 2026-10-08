"""Matching an image's packages against OSV, the open vulnerability database.

OSV answers for every ecosystem an SBOM names (Go, npm, PyPI, Maven, crates,
Alpine, Debian) with one keyless API, so an image's package list is checked in
one request per thousand packages, and each vulnerability's detail once. The
packages come from the SBOM its publisher attached (``application.attestations``);
nothing here touches the image.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, quote, unquote

API = "https://api.osv.dev/v1"
TIMEOUT_SECONDS = 20
# OSV's own limit on one batch.
BATCH = 1000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class OSVReadError(Exception):
    """OSV could not be read, in words a person can act on."""


def _parsed(purl: str) -> tuple[str, str, str, str, dict[str, str]] | None:
    """``(type, namespace, name, version, qualifiers)`` of a package URL."""

    body, _, query = str(purl).removeprefix("pkg:").partition("?")
    body = body.partition("#")[0]
    path, _, version = body.rpartition("@")
    if not path or not version:
        return None
    kind, _, rest = path.partition("/")
    namespace, _, name = rest.rpartition("/")
    qualifiers = {key: values[0] for key, values in parse_qs(query).items() if values}
    return kind.lower(), unquote(namespace), unquote(name), unquote(version), qualifiers


def _distro(qualifiers: Mapping[str, str], prefix: str) -> str:
    """The release a distribution qualifier names: ``3.21`` from ``os_version``
    or ``distro=alpine-3.21``."""

    if qualifiers.get("os_version"):
        return qualifiers["os_version"]
    return qualifiers.get("distro", "").removeprefix(f"{prefix}-")


def query_of(purl: str) -> dict[str, Any] | None:
    """The OSV query for one package, or None for an ecosystem OSV cannot match.

    OSV keys distribution packages by release (``Alpine:v3.21``, ``Debian:12``)
    and Debian ones by source package, which a package URL keeps in a
    qualifier; everything else it matches by the package URL itself, without
    qualifiers.
    """

    parsed = _parsed(purl)
    if parsed is None:
        return None
    kind, namespace, name, version, qualifiers = parsed
    if kind == "apk" and namespace == "alpine":
        release = ".".join(_distro(qualifiers, "alpine").split(".")[:2])
        return {"package": {"ecosystem": f"Alpine:v{release}", "name": name}, "version": version} if release else None
    if kind == "deb" and namespace == "debian":
        release = _distro(qualifiers, "debian").split(".")[0]
        source = qualifiers.get("upstream", "").partition("@")[0] or name
        return {"package": {"ecosystem": f"Debian:{release}", "name": source}, "version": version} if release else None
    if kind in ("apk", "deb", "rpm", "generic", "docker", "oci"):
        return None
    head = f"pkg:{kind}/{quote(namespace, safe='/')}/{quote(name)}" if namespace else f"pkg:{kind}/{quote(name)}"
    return {"package": {"purl": f"{head}@{quote(version, safe='')}"}}


def _post(path: str, payload: Mapping[str, Any]) -> Any:
    return _read(
        urllib.request.Request(
            f"{API}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Severino-HQ"},
            method="POST",
        )
    )


def _get(path: str) -> Any:
    return _read(urllib.request.Request(f"{API}{path}", headers={"User-Agent": "Severino-HQ"}))


def _read(request: urllib.request.Request) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # fixed https host
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OSVReadError(f"OSV returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSVReadError(f"Could not reach OSV: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise OSVReadError("OSV answered with more than HQ reads.")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise OSVReadError("OSV answered with something other than JSON.") from exc


def matches(purls: Iterable[str]) -> tuple[int, dict[str, list[tuple[str, str]]]]:
    """``(packages checked, {purl: [(vulnerability id, modified)]})``."""

    asked = [(purl, query) for purl in purls if (query := query_of(purl)) is not None]
    found: dict[str, list[tuple[str, str]]] = {}
    for start in range(0, len(asked), BATCH):
        chunk = asked[start : start + BATCH]
        answer = _post("/querybatch", {"queries": [query for _purl, query in chunk]})
        for (purl, _query), result in zip(chunk, answer.get("results") or (), strict=False):
            ids = [
                (str(item.get("id", "")), str(item.get("modified", "")))
                for item in (result or {}).get("vulns") or ()
                if item.get("id")
            ]
            if ids:
                found[purl] = ids
    return len(asked), found


def detail(vulnerability_id: str) -> dict[str, Any]:
    return _get(f"/vulns/{quote(vulnerability_id, safe='')}")


def finding(vulnerability: Mapping[str, Any], purl: str, modified: str = "") -> dict[str, Any]:
    """One vulnerability as it bears on one package: what fixes it, how bad."""

    parsed = _parsed(purl)
    kind, _namespace, name, installed, qualifiers = parsed or ("", "", purl, "", {})
    _namespace = unquote(_namespace)
    names = {name, qualifiers.get("upstream", "").partition("@")[0]} - {""}
    fixed: list[str] = []
    severity = str((vulnerability.get("database_specific") or {}).get("severity") or "")
    for entry in vulnerability.get("affected") or ():
        package = entry.get("package") or {}
        if (
            str(package.get("name", "")).rpartition("/")[2] not in names
            and package.get("purl", "").partition("@")[0] != purl.partition("@")[0]
        ):
            continue
        severity = severity or str((entry.get("ecosystem_specific") or {}).get("severity") or "")
        fixed.extend(
            str(event["fixed"])
            for bounds in entry.get("ranges") or ()
            for event in bounds.get("events") or ()
            if event.get("fixed")
        )
    identifier = str(vulnerability.get("id", ""))
    return {
        "id": identifier,
        "package": f"{_namespace}/{name}" if _namespace and kind not in ("apk", "deb") else name,
        "installed": installed,
        "fixed": tuple(dict.fromkeys(fixed)),
        "severity": severity.lower(),
        "summary": str(vulnerability.get("summary") or "")[:300],
        "aliases": tuple(str(alias) for alias in vulnerability.get("aliases") or ())[:8],
        "url": f"https://osv.dev/vulnerability/{quote(identifier, safe='')}",
        "modified": modified or str(vulnerability.get("modified", "")),
    }
