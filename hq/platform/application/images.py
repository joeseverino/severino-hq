"""What an image reference says, and how one version stands against others.

Pure: nothing here reads anything. A container reports its image as a string
(``ghcr.io/example/app:v2.16.0@sha256:…``); this parses it into the
registry, repository, tag and digest it names, and compares versions the way
their own tags are written, so ``1.31.3-alpine`` is compared only with other
``N.N.N-alpine`` tags, never with ``1.33.0`` or ``mainline``.
"""

import re
from dataclasses import dataclass

# Docker's rule: the first path component is a registry when it looks like a
# host, and Docker Hub's official images live under ``library/``.
DOCKER_HUB = "docker.io"
GHCR = "ghcr.io"
_DIGITS = re.compile(r"\d+")
_COMPARATOR = re.compile(r"^\s*(<=|>=|<|>|=)?\s*v?([0-9][0-9A-Za-z.\-+]*)\s*$")


@dataclass(frozen=True, slots=True)
class ImageRef:
    registry: str
    repository: str
    tag: str = ""
    digest: str = ""

    @classmethod
    def parse(cls, reference: str) -> ImageRef | None:
        text = str(reference or "").strip()
        if not text or text.startswith("sha256:"):
            return None
        name, _, digest = text.partition("@")
        first, _, rest = name.partition("/")
        if rest and ("." in first or ":" in first or first == "localhost"):
            registry, path = first, rest
        else:
            registry, path = DOCKER_HUB, name
        tag = ""
        last = path.rsplit("/", 1)[-1]
        if ":" in last:
            path, _, tag = path.rpartition(":")
        if registry == DOCKER_HUB and "/" not in path:
            path = f"library/{path}"
        return cls(registry, path.lower(), tag, digest)

    @property
    def name(self) -> str:
        """``registry/repository``: the one thing every tag of it shares."""

        return f"{self.registry}/{self.repository}" if self.registry else self.repository

    @property
    def short(self) -> str:
        """As people write it: no Docker Hub host, no ``library/``."""

        if not self.registry:
            return self.repository
        if self.registry == DOCKER_HUB:
            return self.repository.removeprefix("library/")
        return self.name

    @property
    def github(self) -> str:
        """``owner/repository`` when the registry is GitHub's own, else ""."""

        if self.registry != GHCR:
            return ""
        parts = self.repository.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else ""


def shape(tag: str) -> str:
    """The tag with every number replaced: ``v2.16.0`` is ``vN.N.N``."""

    return _DIGITS.sub("N", str(tag or ""))


def version(tag: str) -> tuple[int, ...]:
    """The numbers a tag is ordered by: ``1.31.3-alpine`` is ``(1, 31, 3)``.

    Only the leading run of dotted numbers, so a suffix never outranks a
    release. Empty for a tag with none: ``latest`` has no place in an order.
    """

    match = re.match(r"^v?(\d+(?:\.\d+)*)", str(tag or ""))
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _padded(one: tuple[int, ...], other: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(one), len(other))
    return one + (0,) * (width - len(one)), other + (0,) * (width - len(other))


def compare(one: tuple[int, ...], other: tuple[int, ...]) -> int:
    left, right = _padded(one, other)
    return (left > right) - (left < right)


def newer(tag: str, tags) -> list[str]:
    """Tags of the same shape as ``tag`` and a higher version, newest first."""

    running = version(tag)
    if not running:
        return []
    form = shape(tag)
    found = {other for other in tags or () if shape(other) == form and compare(version(other), running) > 0}
    return sorted(found, key=lambda item: _padded(version(item), running)[0], reverse=True)


def affected(tag: str, vulnerabilities) -> bool | None:
    """Whether the version ``tag`` names is affected by an advisory.

    ``vulnerabilities`` are ``(vulnerable range, patched versions)`` pairs as
    GitHub states them: ``(">= 2.0.0, < 2.17.1", "2.17.1")``. A patched version
    at or below the running one clears it even when the range is open-ended,
    because publishers leave ranges open and state the fix beside them. None
    when nothing could be read, so "not known" is never "not affected".
    """

    running = version(tag)
    if not running:
        return None
    readable = False
    for vulnerable, patched in vulnerabilities or ():
        if _patched(running, str(patched or "")):
            readable = True
            continue
        held = _holds(running, str(vulnerable or ""))
        if held is None:
            continue
        readable = True
        if held:
            return True
    return False if readable else None


def _patched(running: tuple[int, ...], text: str) -> bool:
    """Whether a stated fix covers ``running``: the newest fix, or one on the
    same major line as it, at or below it (a fix backported to 1.x does not
    cover 2.0)."""

    fixes = [version(_clean(part)) for part in re.split(r"[,|]", text) if version(_clean(part))]
    if not fixes:
        return False
    newest = max(fixes, key=lambda item: _padded(item, running)[0])
    if compare(running, newest) >= 0:
        return True
    return any(fix[0] == running[0] and compare(running, fix) >= 0 for fix in fixes)


def _clean(text: str) -> str:
    """A version as written by hand: ``v.2.8.0`` and `` 2.8.0 `` are ``2.8.0``."""

    return re.sub(r"^\s*[vV]\.?", "", str(text).strip())


def _holds(running: tuple[int, ...], text: str) -> bool | None:
    text = text.replace("&&", ",").strip()
    # "All" says nothing about which versions: publishers write it for "every
    # version at the time", and a fix released since is never stated beside it.
    between = re.match(r"^\s*(\S+)\s+-\s+(\S+)\s*$", text)
    if between:
        text = f">= {between.group(1)}, <= {between.group(2)}"
    below = re.match(r"^\s*([vV]?\.?\d[\w.]*)\s*<\s*(\S+)\s*$", text)
    if below:
        text = f">= {below.group(1)}, < {below.group(2)}"
    parsed = []
    for part in (part for part in text.split(",") if part.strip()):
        match = _COMPARATOR.match(_clean_comparator(part))
        bound = version(match.group(2)) if match else ()
        if not bound:
            return None
        parsed.append((match.group(1) or "=", compare(running, bound)))
    if not parsed:
        return None
    if all(operator == "=" for operator, _order in parsed):
        # A list of exact versions is any of them, never all at once.
        return any(order == 0 for _operator, order in parsed)
    return all(
        {"<": order < 0, "<=": order <= 0, ">": order > 0, ">=": order >= 0, "=": order == 0}[operator]
        for operator, order in parsed
    )


def _clean_comparator(part: str) -> str:
    match = re.match(r"^\s*(<=|>=|<|>|=)?\s*(.*)$", part)
    return f"{match.group(1) or ''} {_clean(match.group(2))}" if match else part
