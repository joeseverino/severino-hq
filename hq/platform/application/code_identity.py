"""The identity of the code a process runs, for a store that outlives it.

A value pickled by one shape of a class and loaded into another can come back
with its fields misaligned instead of failing, so whatever a process keeps
past its own life is keyed on the code that kept it. The identity is a digest
of the interpreter's version, every installed distribution's name and
version, and the Python source of the host and of every installed extension.
Processes running the same files agree on it; an edit a development server
reloads on, an extension rebuilt into a composed image or an upgraded
dependency moves it.
"""

import hashlib
import os
import sys
from collections.abc import Iterable
from importlib.util import find_spec
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .plugins import installed_plugins

# The host's own top-level packages.
HOST = ("hq", "hq_sdk")
# A distribution's metadata directory is named ``{name}-{version}`` with one of
# these suffixes (the Recording Installed Projects specification), so the
# directory names alone say what is installed and at which version.
_METADATA = (".dist-info", ".egg-info")


def running_code() -> str:
    """The identity of the host and the extensions this process composed."""

    return code_identity((*HOST, *_extension_packages()))


def code_identity(packages: Iterable[str]) -> str:
    """A short digest of the interpreter, the installed distributions and the
    source of the top-level ``packages``."""

    digest = hashlib.sha256(sys.version.encode())
    for installed in _distributions():
        digest.update(f"{installed}\n".encode())
    for package in sorted(set(packages)):
        for name, source in _sources(package):
            digest.update(f"\0{package}/{name}\0".encode())
            digest.update(source.read_bytes())
    return digest.hexdigest()[:16]


def _extension_packages() -> set[str]:
    """The top-level package of every module an installed extension names."""

    return {
        reference.partition(":")[0].split(".")[0]
        for plugin in installed_plugins()
        for reference in (plugin.integration_provider, plugin.urlconf, *plugin.django_apps)
        if reference
    }


def _distributions() -> list[str]:
    """Every installed distribution on the import path, as its metadata
    directory's name, read without opening any of them."""

    return sorted(
        entry.name
        # An empty entry is the working directory.
        for location in map(Path, sys.path)
        if location.is_dir()
        for entry in os.scandir(location)
        if entry.name.endswith(_METADATA)
    )


def _sources(package: str) -> Iterable[tuple[str, Path]]:
    """Every Python file of a top-level package or module, by its path inside
    it, in an order every process walks alike."""

    spec = find_spec(package)
    if spec is None or (spec.submodule_search_locations is None and spec.origin is None):
        raise ImproperlyConfigured(f"{package!r} has no source on the import path, so its code cannot be named.")
    if spec.submodule_search_locations is None:
        origin = Path(spec.origin)
        yield origin.name, origin
        return
    for location in sorted(spec.submodule_search_locations):
        root = Path(location)
        for directory, children, files in root.walk():
            children.sort()
            inside = directory.relative_to(root).as_posix()
            for file in sorted(files):
                if file.endswith(".py"):
                    yield f"{inside}/{file}", directory / file
