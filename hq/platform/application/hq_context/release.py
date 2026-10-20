"""What HQ runs: the commit it was built from, the image that carries it, and
where its repository stands against that."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings

from .tones import ATTENTION, GOOD, NEUTRAL, SERIOUS, Tile, iso
lazy from ..entity_links import web_url
lazy from ..github_estate import repository_for
lazy from ..plugins import installed_plugins


@dataclass(frozen=True, slots=True)
class Release:
    """What HQ runs: the commit it was built from, the image that carries it,
    and where its repository is against that."""

    revision: str
    source: str
    image: str = ""
    digest: str = ""
    built: datetime | None = None
    # Whether the GitHub App reads a signature on the digest that runs; None
    # where nothing was read about the digest.
    signed: bool | None = None
    vulnerabilities: str = ""
    vulnerabilities_note: str = ""
    repository: Any = None
    # ``(name, version)`` of each extension composed into this build, as the
    # running process admitted them.
    extensions: tuple[tuple[str, str], ...] = ()

    @property
    def short(self) -> str:
        return self.revision[:7]

    @property
    def commit_url(self) -> str:
        return f"{self.source}/commit/{self.revision}" if self.source and self.revision else ""

    @property
    def head(self) -> str:
        return str(((self.repository.head if self.repository is not None else None) or {}).get("sha", ""))

    @property
    def deployed(self) -> str:
        production = self.repository.production if self.repository is not None else None
        return str((production or {}).get("sha", ""))

    @property
    def behind(self) -> bool | None:
        """Whether the default branch has moved past production; None unread."""

        return self.repository.undeployed if self.repository is not None else None

    @property
    def runs_deployed(self) -> bool | None:
        """Whether this process is the commit production last deployed."""

        if not self.revision or not self.deployed:
            return None
        return self.deployed.startswith(self.revision) or self.revision.startswith(self.deployed)

    @property
    def standing(self) -> str:
        """Where the release stands, in a few words."""

        if self.behind:
            return f"{self.repository.default_branch or 'Its branch'} is ahead"
        if self.runs_deployed is False:
            return "Not what production last deployed"
        if self.behind is False:
            return "Up to date"
        return "Not known"

    @property
    def tone(self) -> str:
        if self.runs_deployed is False:
            return SERIOUS
        if self.behind:
            return ATTENTION
        return GOOD if self.behind is False else NEUTRAL


def repository(container: Any) -> Any:
    """The repository HQ is built from, as the GitHub App reads it: the one its
    image names, else the one this build says it came from."""

    if container is not None and container.standing.repository is not None:
        return container.standing.repository
    return repository_for(_source())


def _source() -> str:
    return web_url(str(getattr(settings, "SEVERINO_HQ_SOURCE", "") or ""))


def release(container: Any) -> Release:
    standing = container.standing if container is not None else None
    build = standing.build if standing is not None else None
    return Release(
        revision=str(getattr(settings, "SEVERINO_HQ_REVISION", "") or ""),
        source=_source(),
        image=container.running.image if container is not None else "",
        digest=standing.digest if standing is not None else "",
        built=standing.built if standing is not None else None,
        signed=bool(build.get("signed")) if build is not None else None,
        vulnerabilities=standing.vulnerabilities if standing is not None else "",
        vulnerabilities_note=standing.vulnerabilities_note if standing is not None else "",
        repository=repository(container),
        extensions=tuple((plugin.name, plugin.version) for plugin in installed_plugins()),
    )


def release_tile(release: Release) -> Tile:
    return Tile(
        "Release",
        release.short or "Not stamped",
        release.standing if release.revision else "This build does not say which commit it is.",
        release.commit_url,
        release.tone if release.revision else NEUTRAL,
        "deploy",
    )


def release_json(release: Release) -> dict[str, Any]:
    repository = release.repository
    return {
        "revision": release.revision,
        "source": release.source,
        "standing": release.standing,
        "image": release.image,
        "digest": release.digest,
        "built": iso(release.built),
        "signed": release.signed,
        "vulnerabilities": release.vulnerabilities,
        "repository": repository.name if repository is not None else None,
        "default_branch": repository.default_branch if repository is not None else None,
        "head": release.head,
        "deployed": release.deployed,
        "behind": release.behind,
        "runs_deployed": release.runs_deployed,
        "extensions": [{"name": name, "version": version} for name, version in release.extensions],
    }
