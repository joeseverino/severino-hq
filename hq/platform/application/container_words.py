"""A container's standing in the words its row and its page show.

Split by column: what is known against the version that runs, and whether a
newer one is published. ``containers.ImageStanding`` carries the facts and takes
these from here, so each phrase has one owner.
"""

from .images import version

# Whether a newer version is published, apart from what is known against it.
UPDATE_AVAILABLE = "available"
UP_TO_DATE = "current"
UPDATE_UNKNOWN = "unknown"


class ImageStandingWords:
    """The reading of a standing's facts; mixed into ``containers.ImageStanding``."""

    @property
    def checkable(self) -> bool:
        """Whether anything was checked for vulnerabilities: its source's
        advisories, or its packages against OSV."""

        return bool(self.source or self.checked)

    @property
    def vulnerabilities(self) -> str:
        """What is known against the version that runs, in a few words."""

        if self.advisories:
            return f"{len(self.advisories)} known"
        if self.urgent:
            return f"{len(self.urgent)} serious"
        if not self.checkable:
            return "cannot check"
        return "none serious" if self.findings else "none known"

    @property
    def vulnerabilities_note(self) -> str:
        """The sentence behind ``vulnerabilities``, where it needs one."""

        if self.advisories:
            return "Its own project says this version is affected."
        if self.urgent and not self.newer:
            return "A fixed version of each package exists. No release of the image includes it yet."
        if self.urgent:
            return "Critical or high, in packages that have a fixed version."
        if not self.checkable:
            return "The image does not say where its source is, and its publisher provides no package list."
        return ""

    @property
    def update_state(self) -> str:
        """Whether a newer version is published, whatever is known against this one."""

        if self.newer:
            return UPDATE_AVAILABLE
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

        state = self.update_state
        if state == UPDATE_AVAILABLE and self.repository is not None and not self.newer:
            return f"{self.repository.default_branch or 'its branch'} is ahead"
        if state == UPDATE_AVAILABLE:
            return f"{self.latest} available"
        return "up to date" if state == UP_TO_DATE else "unknown"

    @property
    def update_note(self) -> str:
        """Why the update state is unknown, or "" when it is known."""

        if self.update_state != UPDATE_UNKNOWN:
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
