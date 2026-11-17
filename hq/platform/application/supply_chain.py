"""The standard an image's supply chain is held to: whether what runs can be
traced, and whether anything known is wrong with it.

Measured from what HQ reads about the image rather than from the container
(docs/Derived-Facts.md#containers-and-images), all of it anonymously and none
of it touching the image itself, so a check HQ could not read says so rather
than failing.
"""

from typing import Any

from .standards import Check, Measurement, Reading, measure

IMAGE_UNREAD = "Its image has not been read"


def _attested(test):
    def check(container: Any) -> Reading:
        attested = container.standing.attested
        if attested is None or attested.get("unread"):
            return Reading.unavailable("Its attestations have not been read")
        return Reading.judged(test(attested))

    return check


def _pinned(container: Any) -> Reading:
    return Reading.judged(container.standing.pinned)


def _source_known(container: Any) -> Reading:
    standing = container.standing
    if standing.source:
        return Reading.met()
    return Reading.unavailable(IMAGE_UNREAD) if standing.read_at is None else Reading.unmet()


def _no_urgent(container: Any) -> Reading:
    standing = container.standing
    if standing.checked is None:
        return Reading.unavailable("Its packages have not been checked for vulnerabilities")
    return Reading.judged(not standing.urgent)


def _no_advisory(container: Any) -> Reading:
    standing = container.standing
    if not standing.source or standing.upstream_read_at is None:
        return Reading.unavailable("Its project's advisories have not been read")
    return Reading.judged(not standing.advisories)


def _tag_current(container: Any) -> Reading:
    standing = container.standing
    if standing.read_at is None or not standing.tag:
        return Reading.unavailable(IMAGE_UNREAD)
    return Reading.judged(not standing.moved_to)


STANDARD: tuple[Check, ...] = (
    Check(
        "pinned",
        "Pinned by digest",
        _pinned,
        "A tag can later point at a different image. A digest always names the same build.",
        "Pin the image by digest in the compose file.",
        unmet="Not pinned by digest",
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
