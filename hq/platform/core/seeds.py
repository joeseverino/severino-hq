"""The data sets HQ's tools run on, each by one name.

``dev:scratch --seed``, ``demo:build``, the example estate's browser gates and
``dev:look`` name a data set here and take it whole: what it writes and the
settings it is shown under. The sets that ship are registered below; the
browser gate's stress estates register beside their builders in the test
tree, so nothing the server imports reaches test code, and a name is known
wherever its builder has been imported.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

lazy from django.test import override_settings

lazy from hq.platform.core import bench, example_estate


@dataclass(frozen=True)
class Seed:
    """A data set: ``plant`` writes it into an empty database, owned by the
    user it is given. Each of ``settings`` is a setting HQ reads from the
    environment variable of the same name, so a server takes them as its
    environment and a test as overridden settings."""

    plant: Callable[[Any], object]
    settings: Mapping[str, str] = MappingProxyType({})


def _example_estate(owner) -> None:
    example_estate.build(owner)


def _empty(owner) -> None:
    """A fresh install: its one signed-up user, and nothing else."""


def _scale(owner) -> None:
    bench.seed(owner=owner)


# Each builder is reached through a function here, so reading the registry for
# its names or its settings imports none of them.
SEEDS: dict[str, Seed] = {
    # The example estate under Advanced HQ Posture, the public demo's default.
    "showcase": Seed(_example_estate, MappingProxyType({"SEVERINO_POSTURE": "advanced"})),
    "standard": Seed(_example_estate),
    "empty": Seed(_empty),
    # Years of records, to measure pages at the size they grow to.
    "scale": Seed(_scale),
}


def register(name: str, seed: Seed) -> None:
    """Add a data set defined outside the shipped tree, under a name of its own."""

    if name in SEEDS:
        raise ValueError(f"A data set is already named {name!r}.")
    SEEDS[name] = seed


def get(name: str) -> Seed:
    """The data set named ``name``; an unknown name is refused with the known ones."""

    try:
        return SEEDS[name]
    except KeyError:
        raise LookupError(f"No data set is named {name!r}; the known ones are {', '.join(SEEDS)}.") from None


def build(name: str):
    """The named set in this database under its example owner, who is returned."""

    owner = example_estate.example_owner()
    get(name).plant(owner)
    return owner


@contextmanager
def built(name: str) -> Iterator[Any]:
    """The named set built and its settings in force, yielding its owner: a
    page rendered in the block is the one a server on that set answers."""

    with override_settings(**get(name).settings):
        yield build(name)
