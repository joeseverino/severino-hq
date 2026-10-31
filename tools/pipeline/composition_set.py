"""The composition set: which extensions Compose builds HQ with, and their names masked in its log.

``write SET`` checks the shape of COMPOSITION_EXTENSIONS and writes it to SET;
``mask SET`` prints an ``::add-mask::`` for every spelling of every extension.
Stdlib only: the job that runs it sets up no toolchain.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

PATH = re.compile(r"[A-Za-z0-9._/-]+")


class Refused(Exception):
    """A composition set Compose cannot build; the message is a public annotation."""


def given(value: object, fallback: object) -> object:
    """jq's ``value // fallback``: only null and false fall back."""
    return fallback if value is None or value is False else value


def relative(path: object) -> bool:
    """A relative path inside the extension's repository."""
    return (
        isinstance(path, str)
        and PATH.fullmatch(path) is not None
        and not path.startswith("/")
        and "/../" not in f"/{path}/"
    )


def check(text: str) -> None:
    if not text:
        raise Refused(
            "COMPOSITION_EXTENSIONS must be set as a repository secret. A repository variable cannot hold it:"
            " this log is public and only a secret is masked."
        )
    try:
        composition = json.loads(text)
    except ValueError:
        composition = None
    # An empty list is a choice: the host with no extensions. A missing or
    # misspelled one is not, so the key itself must be there.
    extensions = composition.get("extensions") if isinstance(composition, dict) else None
    if not isinstance(extensions, list) or not all(isinstance(extension, dict) for extension in extensions):
        raise Refused("COMPOSITION_EXTENSIONS must be JSON with an extensions list, empty for none")
    # What the deploy may drop. Production refuses an image without an
    # extension it is running unless the image's composition retired it here
    # (controller/host/extensions.go), so an extension leaves only by being
    # named, and never by being both kept and retired.
    retired = given(composition.get("retired"), [])
    if (
        not isinstance(retired, list)
        or not all(isinstance(name, str) and name for name in retired)
        or any(extension.get("id") in retired for extension in extensions)
    ):
        raise Refused("COMPOSITION_EXTENSIONS must list retired extensions as ids it no longer composes")
    # Where in its repository an extension's package is, for a branch built
    # here: "." when omitted.
    if not all(relative(given(extension.get("path"), ".")) for extension in extensions):
        raise Refused("COMPOSITION_EXTENSIONS must give each extension's path as a relative path inside its repository")


def names(composition: dict) -> Iterator[str]:
    """Every name an extension goes by."""
    for extension in composition["extensions"]:
        plugin_id, repository = str(extension.get("id")), str(extension.get("repository"))
        yield from (
            plugin_id,
            repository,
            repository.rsplit("/", maxsplit=1)[-1],
            plugin_id.rsplit(".", maxsplit=1)[-1],
        )
        if (path := str(given(extension.get("path"), ".")).split("/")[-1]) != ".":
            yield path
    for name in given(composition.get("retired"), []):
        yield from (name, name.split(".")[-1])


def masks(composition: dict) -> list[str]:
    """Each name with - or _, in lower, Title and UPPER case, as a title or constant spells it.

    Later steps run gh, uv and Django, which echo repository names, wheel
    filenames, app labels and (when a test fails) table and column names into a
    log that is public.
    """
    spellings = set()
    for name in {name.replace("\r", "") for name in names(composition)} - {""}:
        for spelling in (name, name.replace("-", "_"), name.replace("_", "-")):
            spellings |= {spelling, spelling[:1].upper() + spelling[1:], spelling.upper()}
    return sorted(spellings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("write", "mask"))
    parser.add_argument("set", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.command == "mask":
        for spelling in masks(json.loads(arguments.set.read_text())):
            print(f"::add-mask::{spelling}")
        return 0
    # From the environment, never an argument or the step's text: Actions masks only
    # secret values, so anything private reaches the log that way or not at all.
    text = os.environ.get("COMPOSITION_EXTENSIONS", "")
    try:
        check(text)
    except Refused as refused:
        print(f"::error::{refused}")
        return 1
    arguments.set.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
