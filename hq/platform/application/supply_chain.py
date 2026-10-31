"""The standard an image's supply chain is held to: whether what runs can be
traced, and whether anything known is wrong with it.

Measured from what HQ reads about the image rather than from the container
(docs/Derived-Facts.md#containers-and-images), all of it anonymously and none
of it touching the image itself, so a check HQ could not read says so rather
than failing.
"""

from typing import Any

from .standards import Check, Measurement, measure


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
    Check(
        "pinned",
        "Locked to an exact build",
        lambda container: container.standing.pinned,
        "A tag can later point at a different image. A digest always names the same build.",
        "Pin the image by digest in the compose file.",
        unmet="Not locked to an exact build",
    ),
    Check(
        "source-known",
        "Source repository known",
        _source_known,
        "Without it, no release or advisory can be matched to what runs.",
        "Name the repository it is built from in its settings in HQ.",
        unmet="Source repository unknown",
    ),
    Check(
        "provenance",
        "Has build provenance",
        _attested(lambda attested: bool(attested.get("provenance"))),
        "Provenance says what it was built from and on, so a rebuild or a swapped base image shows.",
        "Only its publisher can attach it.",
        unmet="No build provenance",
    ),
    Check(
        "sbom",
        "Has a package list (SBOM)",
        _attested(lambda attested: bool(attested.get("packages"))),
        "A package list is what vulnerabilities are matched against.",
        "Only its publisher can attach it.",
        unmet="No package list (SBOM)",
    ),
    Check(
        "no-urgent",
        "No fixable serious vulnerability",
        _no_urgent,
        "It has a critical or high vulnerability in a package that has a fixed version.",
        "Upgrade to a release built with the fixed packages.",
        serious=True,
        unmet="Has a fixable serious vulnerability",
    ),
    Check(
        "no-advisory",
        "No advisory against its version",
        _no_advisory,
        "Its own project says this version is affected.",
        "Upgrade past the patched version.",
        serious=True,
        unmet="An advisory names its version",
    ),
    Check(
        "tag-current",
        "Tag still points at the running build",
        _tag_current,
        "The tag was rebuilt or moved since this was pulled, often for a fix.",
        "Pull it again, or pin the new digest.",
        unmet="The tag has moved since this build was pulled",
    ),
)


def supply_chain_of(container: Any) -> Measurement:
    return measure(container, STANDARD)
