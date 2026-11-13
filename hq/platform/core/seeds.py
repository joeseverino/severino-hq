"""The data sets HQ's tools run on, each by one name: what it writes, and the
settings it is shown under. The browser gate's stress estates register beside
their builders in the test tree, which nothing here imports.

The showcase and standard sets are the example estate and each installed
extension's demo (``PluginIntegration.demo_seed``), so a composed server or
look draws an extension's pages with records in them. Every set is written
through ``plant``, which holds two promises:

- it writes only into a database that holds no record of the host's domains,
  so it never merges into one in use, production's included (an extension's
  own tables are not read, since its migrations may write rows of their own);
- an extension's demo is written only in a development or sandboxed process
  (``DEBUG`` on, or a test run's or gate's own database), so even a new
  production database never takes one.

A demo runs outside every door, so an audited row it writes is refused unless
it says how it was made (``seeded_by_hand``), as each of the example estate's
events does (``example_estate.doors``).
"""

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid7

from django.apps import apps
from django.conf import settings
lazy from django.test import override_settings

lazy from hq.config import sandbox
lazy from hq.platform.application.plugins import installed_integrations
lazy from hq.platform.core import bench, example_estate
lazy from hq.platform.core.attribution import outside_every_door
lazy from hq.platform.core.audit import stamped
lazy from hq.platform.core.middleware import request_operation

# The owner of the set whose extension demo is being written, set only while one is.
_DEMO_OWNER: ContextVar[Any] = ContextVar("hq_seed_demo_owner", default=None)

# Records are what HQ's domains keep. Users, sessions and the platform's own
# bookkeeping are not.
_DOMAINS = "hq.domains."


class SeedRefused(Exception):
    """A set this database or this process does not take, and why; nothing was written."""


@dataclass(frozen=True)
class Seed:
    """``plant`` writes the set into an empty database, owned by the user it
    is given. Each setting is read from the environment variable of its name,
    so a server takes them as its environment and a test as settings."""

    plant: Callable[[Any], object]
    settings: Mapping[str, str] = frozendict()


def records_held() -> dict[str, int]:
    """Each domain model that holds rows, by its label, with how many."""

    held = {}
    for model in apps.get_models():
        if model.__module__.startswith(_DOMAINS) and not model._meta.proxy:
            count = model._default_manager.count()
            if count:
                held[model._meta.label] = count
    return held


def counted(held: dict[str, int]) -> str:
    return ", ".join(f"{label} ({count})" for label, count in sorted(held.items()))


def _sandboxed() -> bool:
    """Whether this process writes to a development database or a run's own, never a deployment's."""

    return settings.DEBUG or sandbox.RUN in os.environ


def _demos() -> list[Callable[[Any], object]]:
    """Each installed extension's demo seed, asked before anything is written,
    so a process that may not write one is refused with the database untouched."""

    found = [integration.demo_seed for _, integration in installed_integrations() if integration.demo_seed]
    if found and not _sandboxed():
        raise SeedRefused(
            "An extension's demo is written only by a development server or a test run, so nothing was seeded: "
            "set DJANGO_DEBUG=1 for a database of its own."
        )
    return found


def demo_pages() -> list[str]:
    """A record's page of each kind the installed extensions' demos write, by address."""

    return [
        address
        for _, integration in installed_integrations()
        if integration.demo_pages is not None
        for address in integration.demo_pages()
    ]


def _estate(owner) -> None:
    """The example estate, and each installed extension's demo beside it."""

    demos = _demos()
    example_estate.build(owner)
    for demo in demos:
        with _DEMO_OWNER.set(owner), outside_every_door():
            demo(owner)


def _read_empty(owner) -> None:
    """The example estate the moment its Docker environment answered a read
    with no containers, so the claim HQ makes about that is drawn."""

    _estate(owner)
    example_estate.docker.read_empty()


def _reading(owner) -> None:
    """The example estate with every connection only reading, as an HQ whose
    credentials may not make changes runs: each page that offers a change
    says what it needs instead."""

    _estate(owner)
    apps.get_model("control_plane", "ProviderConnection").objects.update(manages=False)


def _rescanned(owner) -> None:
    """The example estate once a later Refresh found the commit adopted, so
    the procedures page has nothing to adopt."""

    _estate(owner)
    example_estate.automation.rescanned(owner)


def _scanning(owner) -> None:
    """The example estate while a Refresh is under way, so the procedures
    page follows a scan."""

    _estate(owner)
    example_estate.automation.scanning(owner)


@contextmanager
def seeded_by_hand(path: str, *, at: datetime) -> Iterator[None]:
    """Each audit event the block records, written as the set's owner made it
    by hand: in the browser, one request for ``path``, at ``at``.

    For an extension's demo, whose records are written in one moment but stand
    for months of use. Writing an event as somebody, at a moment past, is a
    seed's power alone, so it is refused anywhere but inside a demo the
    showcase or standard set is writing, which only a development or sandboxed
    process does.
    """

    owner = _DEMO_OWNER.get()
    if owner is None:
        raise SeedRefused(
            "Only an extension's demo, while the showcase or standard set writes it, records events made by hand."
        )
    with (
        request_operation(path, operation_id=uuid7().hex),
        stamped(lambda _record, _action, _object_type: {"user": owner, "created_at": at}),
    ):
        yield


# Each builder is reached through a function, so reading the registry imports none.
# The example estate is a set-up HQ, so public DNS changes are on, as its installer sets them.
_SET_UP = frozendict({"SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS": "1"})

SEEDS: dict[str, Seed] = {
    "showcase": Seed(_estate, frozendict({**_SET_UP, "SEVERINO_POSTURE": "advanced"})),
    "standard": Seed(_estate, _SET_UP),
    "reading": Seed(_reading, _SET_UP),
    "read-empty": Seed(_read_empty, _SET_UP),
    "rescanned": Seed(_rescanned, _SET_UP),
    "scanning": Seed(_scanning, _SET_UP),
    # A fresh install: its one signed-up user, and nothing else.
    "empty": Seed(lambda _owner: None),
    # Years of records, to measure pages at the size they grow to.
    "scale": Seed(lambda owner: bench.seed(owner=owner)),
}


def register(name: str, seed: Seed) -> None:
    if name in SEEDS:
        raise ValueError(f"A data set is already named {name!r}.")
    SEEDS[name] = seed


def get(name: str) -> Seed:
    if name not in SEEDS:
        raise LookupError(f"No data set is named {name!r}; the known ones are {', '.join(SEEDS)}.")
    return SEEDS[name]


def plant(name: str, owner) -> None:
    """The named set written into this database, owned by ``owner``; refused
    whole, before anything is written, by a database that holds a record."""

    seed = get(name)
    # Merging example rows into real ones would leave a database that is
    # neither, so one that already holds a record is refused and left as it was.
    if held := records_held():
        raise SeedRefused(
            f"This database already holds records, so nothing was seeded: {counted(held)}. "
            "seed fills an empty database and never merges, so a new database takes this set."
        )
    seed.plant(owner)


@contextmanager
def built(name: str) -> Iterator[Any]:
    """The named set in this database, owned by the example owner it yields,
    with its settings in force: a page rendered in the block is the one a
    server on that set answers."""

    with override_settings(**get(name).settings):
        owner = example_estate.example_owner()
        plant(name, owner)
        yield owner
