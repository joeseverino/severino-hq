"""The data sets HQ's tools run on, each by one name: what it writes, and the
settings it is shown under. The browser gate's stress estates register beside
their builders in the test tree, which nothing here imports."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

lazy from django.test import override_settings

lazy from hq.platform.core import bench, example_estate


@dataclass(frozen=True)
class Seed:
    """``plant`` writes the set into an empty database, owned by the user it
    is given. Each setting is read from the environment variable of its name,
    so a server takes them as its environment and a test as settings."""

    plant: Callable[[Any], object]
    settings: Mapping[str, str] = MappingProxyType({})


# Each builder is reached through a function, so reading the registry imports none.
SEEDS: dict[str, Seed] = {
    "showcase": Seed(lambda owner: example_estate.build(owner), MappingProxyType({"SEVERINO_POSTURE": "advanced"})),
    "standard": Seed(lambda owner: example_estate.build(owner)),
    # A fresh install: its one signed-up user, and nothing else.
    "empty": Seed(lambda owner: None),
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


@contextmanager
def built(name: str) -> Iterator[Any]:
    """The named set in this database, owned by the example owner it yields,
    with its settings in force: a page rendered in the block is the one a
    server on that set answers."""

    with override_settings(**get(name).settings):
        owner = example_estate.example_owner()
        get(name).plant(owner)
        yield owner
