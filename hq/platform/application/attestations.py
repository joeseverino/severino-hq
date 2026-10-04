"""What an image's publisher attached to it, reduced to what HQ uses.

BuildKit attaches in-toto statements beside each platform's manifest: an SBOM
(SPDX or CycloneDX) listing every package in the image, and SLSA provenance
saying where and from what it was built. Both are the publisher's word and
unsigned, so a page says "states" rather than "proves". HQ keeps the package
URLs and a handful of provenance fields; the statements themselves are dropped.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .github_public import github_repository

SPDX = "https://spdx.dev/Document"
CYCLONEDX = "https://cyclonedx.org/bom"
SLSA_V02 = "https://slsa.dev/provenance/v0.2"
SLSA_V1 = "https://slsa.dev/provenance/v1"
_BUILDKIT = "https://mobyproject.org/buildkit@v1#metadata"
# git@github.com:owner/repository.git, the form a local clone reports.
_SCP = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$")


def packages_of(predicate_type: str, predicate: Mapping[str, Any]) -> tuple[str, ...]:
    """Every package URL an SBOM names, once, in order."""

    if predicate_type == SPDX:
        found: Iterable[str] = (
            str(reference.get("referenceLocator", ""))
            for package in predicate.get("packages") or ()
            for reference in package.get("externalRefs") or ()
            if reference.get("referenceType") == "purl"
        )
    elif predicate_type == CYCLONEDX:
        found = (str(component.get("purl", "")) for component in _components(predicate.get("components") or ()))
    else:
        return ()
    return tuple(dict.fromkeys(purl for purl in found if purl.startswith("pkg:")))


def _components(components) -> Iterable[Mapping[str, Any]]:
    for component in components:
        if isinstance(component, dict):
            yield component
            yield from _components(component.get("components") or ())


def provenance_of(predicate_type: str, predicate: Mapping[str, Any]) -> dict[str, Any]:
    """Where it says it was built from, at which commit, by what, on what."""

    if predicate_type == SLSA_V02:
        metadata = predicate.get("metadata") or {}
        vcs = (metadata.get(_BUILDKIT) or {}).get("vcs") or {}
        # The workflow's repository is where it was built, not what from: a
        # release pipeline builds another repository's code.
        environment = (predicate.get("invocation") or {}).get("environment") or {}
        return {
            "format": "SLSA 0.2",
            "source": str(vcs.get("source") or ""),
            "revision": str(vcs.get("revision") or environment.get("github_sha") or ""),
            "builder": _builder(str((predicate.get("builder") or {}).get("id") or ""), environment),
            "finished_at": str(metadata.get("buildFinishedOn") or ""),
            "materials": _materials(predicate.get("materials") or ()),
        }
    if predicate_type == SLSA_V1:
        definition = predicate.get("buildDefinition") or {}
        details = predicate.get("runDetails") or {}
        metadata = details.get("metadata") or {}
        vcs = (metadata.get("buildkit_metadata") or {}).get("vcs") or {}
        return {
            "format": "SLSA 1.0",
            "source": str(vcs.get("source") or ""),
            "revision": str(vcs.get("revision") or ""),
            "builder": _builder(str((details.get("builder") or {}).get("id") or ""), {}),
            "finished_at": str(metadata.get("finishedOn") or ""),
            "materials": _materials(definition.get("resolvedDependencies") or ()),
        }
    return {}


def _builder(builder_id: str, environment: Mapping[str, Any]) -> str:
    """The builder, named as a person would: a GitHub Actions run, or its id."""

    if builder_id.startswith("https://github.com/") and "/actions/runs/" in builder_id:
        return builder_id
    if environment.get("github_run_id") and environment.get("github_repository"):
        return f"https://github.com/{environment['github_repository']}/actions/runs/{environment['github_run_id']}"
    return builder_id


def _materials(items) -> tuple[str, ...]:
    return tuple(
        str(item.get("uri", ""))
        for item in items
        if isinstance(item, dict) and str(item.get("uri", "")).startswith("pkg:docker/")
    )


def github_source(source: str) -> str:
    """``owner/repository`` when a provenance source is a GitHub repository."""

    text = str(source or "").strip()
    scp = _SCP.match(text)
    if scp:
        return f"{scp.group(1)}/{scp.group(2)}"
    named = github_repository(text.removesuffix(".git"))
    return "/".join(named) if named else ""


def reduce(statements: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """``{packages, sbom, provenance}`` from the statements attached to one digest."""

    packages: tuple[str, ...] = ()
    sbom = ""
    provenance: dict[str, Any] | None = None
    for predicate_type, predicate in statements:
        if predicate_type in (SPDX, CYCLONEDX) and not sbom:
            packages = packages_of(predicate_type, predicate)
            sbom = "SPDX" if predicate_type == SPDX else "CycloneDX"
        elif predicate_type in (SLSA_V02, SLSA_V1) and provenance is None:
            provenance = provenance_of(predicate_type, predicate)
    return {"packages": packages, "sbom": sbom, "provenance": provenance}
