"""The standard an image's supply chain is held to: whether what runs can be
traced, and whether anything known is wrong with it.

Measured from what HQ reads about the image rather than from the container
(docs/Derived-Facts.md#containers-and-images), all of it anonymously and none
of it touching the image itself, so a check HQ could not read says so rather
than failing. What only its publisher can attach (a package list, build
provenance) is a fact on its page, never a check its operator could fail.
"""

from datetime import UTC, datetime
from typing import Any

from hq.domains.control_plane.observations.public_registry import IMAGE_KIND

from .standards import Check, Examples, Measurement, Reading, measure
from .ui import counted
lazy from .container_standard import example

IMAGE_UNREAD = "Its image has not been read"


def _pulled(container: Any) -> bool:
    """Whether a registry holds what runs, so a pin, a source and a tag
    apply to it: an image built on its host has none."""

    return not container.standing.built_here


def _pinned(container: Any) -> Reading:
    return Reading.judged(container.standing.pinned)


def _source_known(container: Any) -> Reading:
    standing = container.standing
    if standing.source:
        return Reading.met()
    return Reading.unavailable(IMAGE_UNREAD) if standing.read_at is None else Reading.unmet()


def _no_urgent(container: Any) -> Reading:
    """Unmet only when the release an upgrade would pin is clear of one: a
    serious vulnerability no release fixes is said, and failed once one does."""

    standing = container.standing
    if standing.checked is None:
        return Reading.unavailable("Its packages have not been checked for vulnerabilities")
    if standing.urgent:
        return Reading.unmet()
    if standing.unfixed and standing.fix_unread:
        return Reading.unavailable(f"The packages of {standing.latest} have not been checked")
    if standing.unfixed:
        count = counted(len(standing.unfixed), "serious vulnerability", "serious vulnerabilities")
        return Reading.met(f"{count}, {standing.unfixed_said}", limited=True)
    return Reading.met()


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
        applies=_pulled,
        unmet="Not pinned by digest",
        breaks=({"pinned": False},),
    ),
    Check(
        "source-known",
        "Source repository known",
        _source_known,
        "Without it, no release or advisory can be matched to what runs.",
        "Name the repository it is built from in its settings in HQ.",
        applies=_pulled,
        unmet="Source repository unknown",
        breaks=({"source": ""},),
    ),
    Check(
        "no-urgent",
        "No fixable serious vulnerability",
        _no_urgent,
        "It has a critical or high vulnerability that its newest release fixes.",
        "Upgrade to its newest release.",
        serious=True,
        unmet="Has a serious vulnerability its newest release fixes",
        breaks=(
            {
                "newer": ("1.3.0",),
                "checked": {"findings": [{"id": "GHSA-1", "severity": "high", "fixed": ["1.2.1"]}]},
                "target_checked": {"findings": []},
            },
        ),
        # Only a serious one a release fixes is urgent.
        keeps=(
            {
                "checked": {
                    "findings": [
                        {"id": "GHSA-2", "severity": "low", "fixed": ["1.2.1"]},
                        {"id": "GHSA-3", "severity": "critical", "fixed": ["1.2.1"]},
                    ]
                }
            },
        ),
    ),
    Check(
        "no-advisory",
        "No advisory against its version",
        _no_advisory,
        "Its own project says this version is affected.",
        "Upgrade past the patched version.",
        serious=True,
        unmet="An advisory names its version",
        breaks=({"advisories": ({"id": "GHSA-4"},)},),
    ),
    Check(
        "tag-current",
        "Tag still points at the running build",
        _tag_current,
        "The tag was rebuilt or moved since this was pulled, often for a fix.",
        "Pull it again, or pin the new digest.",
        applies=_pulled,
        unmet="The tag has moved since this build was pulled",
        breaks=({"moved_to": "sha256:bbb"},),
    ),
)


def supply_chain_of(container: Any) -> Measurement:
    return measure(container, STANDARD, reads=IMAGE_KIND)


_READ = datetime(2026, 1, 1, tzinfo=UTC)

# An image whose source, packages and advisories were all read and found sound.
EXAMPLES = Examples(
    checks=STANDARD,
    subject=lambda record: example(**record),
    kept={
        "source": "https://github.com/example/web",
        "read_at": _READ,
        "upstream_read_at": _READ,
        "checked": {"findings": []},
    },
)
