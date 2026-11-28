"""What OSV knows against an image's packages, judged by what a release fixes.

``containers.ImageStanding`` carries the readings and takes these from here,
as it takes its words from ``container_words``.
"""

from collections.abc import Mapping
from typing import Any

# Worst first; a level not named sorts after these.
SEVERITIES = ("critical", "high", "medium", "moderate", "low")
SERIOUS = ("critical", "high")


def ranked(findings) -> tuple[Mapping[str, Any], ...]:
    """Worst first; unrated last."""

    rank = {level: index for index, level in enumerate(SEVERITIES)}
    return tuple(sorted(findings, key=lambda item: rank.get(str(item.get("severity", "")), len(SEVERITIES))))


class ImageFindings:
    """The judgment of a standing's package findings; mixed into ``containers.ImageStanding``."""

    @property
    def findings(self) -> tuple[Mapping[str, Any], ...]:
        """What OSV knows against its packages, worst first."""

        return ranked((self.checked or {}).get("findings") or ())

    @property
    def serious_findings(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(item for item in self.findings if item.get("severity") in SERIOUS)

    @property
    def urgent(self) -> tuple[Mapping[str, Any], ...]:
        """Critical or high, and gone from the packages of the release an
        upgrade would pin: what to act on. One no release is known to fix is
        shown, never raised, since nothing can be done about it yet."""

        if not self.newer or self.target_checked is None:
            return ()
        after = {str(item.get("id", "")) for item in self.target_checked.get("findings") or ()}
        return tuple(item for item in self.serious_findings if str(item.get("id", "")) not in after)

    @property
    def fix_unread(self) -> bool:
        """Whether a newer release is published whose packages are not
        checked, so what it would fix is not known yet."""

        return bool(self.newer) and self.target_checked is None

    @property
    def unfixed(self) -> tuple[Mapping[str, Any], ...]:
        """Critical or high, each once by its id, that no release is known to fix."""

        urgent = {str(item.get("id", "")) for item in self.urgent}
        found: dict[str, Mapping[str, Any]] = {}
        for item in self.serious_findings:
            if str(item.get("id", "")) not in urgent:
                found.setdefault(str(item.get("id", "")), item)
        return tuple(found.values())

    @property
    def packaged_fix(self) -> tuple[Mapping[str, Any], ...]:
        """Of ``unfixed``, those whose package names a version that fixes it:
        fixed where the package is made, in no release of the image yet."""

        return tuple(item for item in self.unfixed if item.get("fixed"))

    @property
    def actionable(self) -> bool:
        """Whether something known against it is the image's to act on: its
        own project's advisory, or a serious finding a newer release clears."""

        return bool(self.advisories or self.urgent)

    @property
    def known_vulnerabilities(self) -> tuple[Mapping[str, Any], ...]:
        """Everything serious known against it, each once by its id: its
        advisories, then every critical or high package finding, whether or
        not a release fixes it. Every count of its vulnerabilities (a row, a
        title, a band's tally, what an upgrade fixes of them) is this one's,
        so no two can disagree. Worst first."""

        found: dict[str, Mapping[str, Any]] = {}
        for item in (*self.advisories, *self.serious_findings):
            found.setdefault(str(item.get("id", "")), item)
        return ranked(found.values())
