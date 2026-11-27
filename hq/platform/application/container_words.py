"""A container's standing in the words its row and its page show.

Split by column: what is known against the version that runs, and whether a
newer one is published. ``containers.ImageStanding`` carries the facts and takes
these from here, so each phrase has one owner.
"""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from django.utils.text import capfirst, get_text_list

from .card_items import Answer
from .entity_links import EntityLink, declared_link
from .images import version
from .ui import counted

# Whether a newer version is published, apart from what is known against it.
UPDATE_AVAILABLE = "available"
UP_TO_DATE = "current"
UPDATE_UNKNOWN = "unknown"


class ImageStandingWords:
    """The reading of a standing's facts; mixed into ``containers.ImageStanding``."""

    @property
    def checkable(self) -> bool:
        """Whether anything was checked for vulnerabilities: its source's
        advisories, or its packages against OSV. A source named but never
        read has checked nothing."""

        return self.upstream_read_at is not None or bool(self.checked)

    @property
    def awaiting_read(self) -> bool:
        """Whether its source is known and its advisories are only not read yet."""

        return bool(self.source) and not self.checkable

    @property
    def vulnerabilities(self) -> str:
        """What is known against the version that runs, in a few words."""

        if self.advisories:
            return f"{len(self.known_vulnerabilities)} known"
        if self.urgent:
            return f"{len(self.known_vulnerabilities)} serious"
        if self.awaiting_read:
            return "not read"
        if not self.checkable:
            return "cannot check"
        if self.unfixed:
            return f"{len(self.unfixed)} serious" + ("" if self.fix_unread else ", no fix yet")
        return "none serious" if self.findings else "none known"

    @property
    def vulnerabilities_note(self) -> str:
        """The sentence behind ``vulnerabilities``, where it needs one."""

        if self.advisories:
            return "Its own project says this version is affected."
        if self.urgent:
            return f"Critical or high. {self.latest} fixes them."
        if self.awaiting_read:
            return self.unread or "HQ has not read its source's advisories yet."
        if not self.checkable:
            return "The image does not say where its source is, and its publisher provides no package list."
        if self.unfixed and self.fix_unread:
            return f"The packages of {self.latest} are not checked yet."
        if self.unfixed:
            return "No release fixes them yet."
        return self.checked_against

    @property
    def checked_against(self) -> str:
        """What was checked for what is known against it, as a reason says it."""

        parts = [
            *((counted(int(self.checked.get("checked") or 0), "package"),) if self.checked else ()),
            *(("its project's advisories",) if self.upstream_read_at is not None else ()),
        ]
        return f"Checked {get_text_list(parts, 'and')}" if parts else ""

    @property
    def vulnerabilities_said(self) -> Answer:
        """``vulnerabilities`` as a fact says it, over its sentence and the
        first of what is known against it, in its tone."""

        minor = {str(item.get("id", "")) for item in self.findings if item not in self.serious_findings}
        lower = (f"{len(minor)} of lower severity",) if minor and not self.urgent else ()
        return Answer(
            capfirst(self.vulnerabilities),
            (*lower, self.vulnerabilities_note, *named(self.known_vulnerabilities or self.unfixed)),
            self.vulnerability_tone,
        )

    @property
    def update_state(self) -> str:
        """Whether a newer version is published, whatever is known against this one."""

        if self.newer:
            return UPDATE_AVAILABLE
        if self.built_here:
            # Its repository's open pull request or a merge its host has not
            # built says it is behind (``app_updates.update_state``), never a registry.
            return UPDATE_UNKNOWN
        if self.repository is not None:
            # Built by a repository HQ reads: its registry may refuse an
            # anonymous read, and its release pipeline, not a tag, says what
            # is newer. Newer is the default branch moving past production.
            behind = self.repository.undeployed
            return UPDATE_UNKNOWN if behind is None else UPDATE_AVAILABLE if behind else UP_TO_DATE
        if self.build is not None and self.build.get("signed"):
            return UP_TO_DATE
        return UP_TO_DATE if self.newest else UPDATE_UNKNOWN

    @property
    def update(self) -> str:
        """The update state in a few words."""

        if self.built_here:
            return "from its repository"
        state = self.update_state
        if state == UPDATE_AVAILABLE and self.repository is not None and not self.newer:
            return f"{self.repository.default_branch or 'its branch'} is ahead"
        if state == UPDATE_AVAILABLE:
            return f"{self.latest} available"
        return "up to date" if state == UP_TO_DATE else "unknown"

    @property
    def update_note(self) -> str:
        """Why the update state is unknown, or "" when it is known."""

        if self.update_state != UPDATE_UNKNOWN or self.built_here:
            return ""
        if self.repository is not None:
            return "Its repository's head or its production deploy has not been read."
        if self.unread:
            return self.unread
        if not self.tag:
            return "Cannot compare. It runs an exact build with no version tag."
        if not version(self.tag):
            return f"Cannot compare. The tag {self.tag} is not a version number."
        return "Its registry has not been read yet."


def named(advisories: tuple[Mapping[str, Any], ...]) -> tuple[EntityLink, ...]:
    """The first two of ``advisories``, each linked by its id with its
    severity and package beside it: a fact's reasons."""

    return tuple(
        replace(
            declared_link(str(each.get("id", "")), str(each.get("url", "")), "dependency"),
            detail=" ".join(
                filter(None, (str(each.get("severity") or ""), f"in {each['package']}" if each.get("package") else ""))
            )
            or "advisory",
        )
        for each in advisories[:2]
    )
