"""The standard an image's supply chain is held to: whether what runs can be
traced, and whether anything known is wrong with it.

Measured from what HQ reads about the image rather than from the container:
its reference, the attestations its publisher attached to the digest that runs
(``registry.digest``), those packages checked against OSV
(``registry.vulnerabilities``) and the advisories on its source repository.
Every one is read anonymously and none of it touches the image itself, so a
check HQ could not read says so rather than failing.
"""

from __future__ import annotations

from typing import Any

from .standards import Check, Posture, measure


def _attested(test):
    def check(container: Any) -> bool | None:
        attested = container.standing.attested
        return None if attested is None or attested.get("unread") else test(attested)

    return check


def _source_known(container: Any) -> bool | None:
    standing = container.standing
    return True if standing.source else (None if standing.read_at is None else False)


def _no_urgent(container: Any) -> bool | None:
    standing = container.standing
    return None if standing.checked is None else not standing.urgent


def _no_advisory(container: Any) -> bool | None:
    standing = container.standing
    return None if not standing.source or standing.upstream_read_at is None else not standing.advisories


def _tag_current(container: Any) -> bool | None:
    standing = container.standing
    return None if standing.read_at is None or not standing.tag else not standing.moved_to


STANDARD: tuple[Check, ...] = (
    Check("pinned", "Pinned to a digest", lambda container: container.standing.pinned,
          "A tag can be moved to a different image; a digest cannot.",
          "Pin the image by digest in the compose file."),
    Check("source-known", "Its source is known", _source_known,
          "Without it, no release or advisory can be matched to what runs.",
          "Name the repository it is built from on its declaration."),
    Check("provenance", "Its build is described", _attested(lambda attested: bool(attested.get("provenance"))),
          "Provenance says what it was built from and on, so a rebuild or a swapped base image shows.",
          "Only its publisher can attach it."),
    Check("sbom", "Its packages are listed", _attested(lambda attested: bool(attested.get("packages"))),
          "A package list is what vulnerabilities are matched against.",
          "Only its publisher can attach it."),
    Check("no-urgent", "No fixable serious vulnerability", _no_urgent,
          "A critical or high vulnerability with a fix published is the one to act on.",
          "Upgrade to a release built with the fixed packages.", serious=True),
    Check("no-advisory", "No advisory against its version", _no_advisory,
          "Its own project says this version is affected.",
          "Upgrade past the patched version.", serious=True),
    Check("tag-current", "Its tag still names what runs", _tag_current,
          "The tag was rebuilt or moved since this was pulled, often for a fix.",
          "Pull it again, or pin the new digest."),
)


def supply_chain_of(container: Any) -> Posture:
    return measure(container, STANDARD)
