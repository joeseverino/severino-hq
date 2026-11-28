"""The data sets HQ's tools run on, each by one name: what it writes, and the settings it is shown under.

Every set is written through ``plant``, which writes only into a database
holding no host record, and writes an extension's demo only in a development or
sandboxed process, so production never takes one.
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
lazy from hq.domains.content.models import ContentItem
lazy from hq.platform.application.action_items import set_aside
lazy from hq.platform.application.dashboard import readable_queue
lazy from hq.platform.application.plugins import installed_integrations
lazy from hq.platform.application.projection import projection_scope
lazy from hq.platform.application.security import web_principal
lazy from hq.platform.core import bench, example_estate
lazy from hq.platform.core.attribution import outside_every_door
lazy from hq.platform.core.audit import stamped
lazy from hq.platform.core.example_estate import update_steps
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


def _images_left(owner) -> None:
    """The example estate with images nothing runs on its machine, so the
    finding about those HQ can remove, and its silence about those it keeps,
    are drawn."""

    _estate(owner)
    example_estate.docker.images_left()


def _advised(owner) -> None:
    """The example estate with advisories against what runs and newer
    releases, so each upgrade a card can offer, or say is not worth making,
    is drawn."""

    _estate(owner)
    example_estate.docker.advised()


def _rescanned(owner) -> None:
    """The example estate once a later scan found the commit adopted, so
    the procedures page has nothing to adopt."""

    _estate(owner)
    example_estate.automation.rescanned()


def _unadoptable(owner) -> None:
    """The example estate once a later scan read a commit HQ can't adopt,
    so the Playbooks page says why for the commit and each playbook."""

    _estate(owner)
    example_estate.automation.unadoptable()


def _partly_adoptable(owner) -> None:
    """The example estate once a later scan read a commit HQ can adopt all
    but one playbook of, so the Playbooks page offers adopting the rest."""

    _estate(owner)
    example_estate.automation.partly_adoptable()


def _unattended(owner) -> None:
    """The example estate once its playbook worker stopped reporting, so the
    Playbooks page and the run that waits say what is missing."""

    _estate(owner)
    example_estate.automation.unattended()


def _worker_image_missing(owner) -> None:
    """The example estate once a release stopped short on HQ's machine: the
    playbook worker's image is not there, so its unit failed and it stopped
    reporting, and the journal's mount never started."""

    _estate(owner)
    _short_release()


def _short_release() -> None:
    example_estate.automation.unattended()
    example_estate.work.worker_image_missing()


def _readings_aged(owner) -> None:
    """The example estate with a reading out of date, one failing and one
    read with a part refused, so each standing the Controller page lists a
    reading in is drawn."""

    _estate(owner)
    example_estate.work.readings_aged()


def _glance_aged(owner) -> None:
    """The example estate as the dashboard finds it after nobody looked for a
    while: the machine's reading due again, nothing asked yet."""

    _estate(owner)
    example_estate.infra.glance_aged()


def _glance_unanswered(owner) -> None:
    """The example estate with the dashboard's ask for the machine's reading
    left unanswered, so the reading is out of date."""

    _estate(owner)
    example_estate.infra.glance_unanswered()


def _glance_asked(owner) -> None:
    """The example estate as the dashboard opens on it cold: the machine's
    reading a day old and the forecast never read, each asked for by the
    dashboard itself and still under way."""

    _estate(owner)
    example_estate.infra.glance_asked()


def _refreshes_asked(owner) -> None:
    """The example estate with refreshes asked of the controller and not
    yet answered beside the changes queued for it."""

    _estate(owner)
    example_estate.infra.refreshes_asked()


def _policy_change(owner) -> None:
    """The example estate with an agent's change to the tailnet policy held,
    one that opens a port and drops the test that kept it shut, so the
    approval says so first."""

    _estate(owner)
    example_estate.agents.asks_to_open_the_policy()


def _told_again(owner) -> None:
    """A fresh install with one problem, its playbook worker stopped, and a
    notice of a draft its owner dismissed, which a newer draft has since told
    again."""

    example_estate.automation.unattended()
    ContentItem.objects.create(title="An example draft", slug="example-draft")
    with projection_scope():
        queue = readable_queue(web_principal(owner))
    set_aside(owner, [item["key"] for item in queue if item["notice"]], aside=True, current=queue)
    ContentItem.objects.create(title="A second example draft", slug="example-second-draft")


def _approved(ended: str) -> Callable[[Any], None]:
    """The example estate with the playbook that applies app updates
    adopted, and its app update approved and ended as ``ended`` says, so its
    container's Upgrade card draws that standing."""

    def seed(owner) -> None:
        _estate(owner)
        example_estate.automation.applies_updates(web_principal(owner), (example_estate.names.MACHINE,))
        update_steps.approval(owner, ended)

    return seed


def _unlisted(owner) -> None:
    _estate(owner)
    example_estate.work.unlisted()


def _updates(on: str = "") -> Callable[[Any], None]:
    """The example estate with an update of each app in a state of its own
    (``example_estate.work.update_states``), applied by a playbook written
    for its machine, or, given ``on``, only for that other machine; the
    agent's approval of the DNS server's update waits for a person."""

    def seed(owner) -> None:
        # Declared first, so every scan the estate reports reaches it.
        if on:
            apps.get_model("control_plane", "ManagedResource").objects.create(key=on, kind="machine", spec={"name": on})
        _estate(owner)
        example_estate.docker.updates_out()
        example_estate.work.update_states(example_estate.docker.UPDATES)
        example_estate.automation.applies_updates(web_principal(owner), (on or example_estate.names.MACHINE,))
        update = example_estate.work.resolver_update(example_estate.docker.UPDATES)
        example_estate.automation.asks_to_approve(update, f"example/{example_estate.work.APPS}")

    return seed


def _opening(at: str) -> Callable[[Any], None]:
    """The update of each app (``_updates``), the one Renovate holds asked to
    open and left as ``at`` says (``update_steps.opening``)."""

    def seed(owner) -> None:
        _updates()(owner)
        update_steps.opening(owner, at)

    return seed


def _checks_running(owner) -> None:
    _updates()(owner)
    update_steps.checks_running()


@contextmanager
def seeded_by_hand(path: str, *, at: datetime) -> Iterator[None]:
    """Each audit event the block records, written as the set's owner made it
    by hand: in the browser, one request for ``path``, at ``at``.

    Writing an event as somebody, in the past, is refused anywhere but inside a
    demo the showcase or standard set is writing.
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
    "advised": Seed(_advised, _SET_UP),
    "images-left": Seed(_images_left, _SET_UP),
    "rescanned": Seed(_rescanned, _SET_UP),
    "unadoptable": Seed(_unadoptable, _SET_UP),
    "partly-adoptable": Seed(_partly_adoptable, _SET_UP),
    "unattended": Seed(_unattended, _SET_UP),
    # A fresh install whose playbook worker stopped reporting: the one serious
    # item, so the dashboard's Needs you card holds it alone, with its actions.
    "playbooks-stopped": Seed(lambda _owner: example_estate.automation.unattended()),
    "told-again": Seed(_told_again),
    "worker-image-missing": Seed(_worker_image_missing, _SET_UP),
    # The same on a fresh install, so the dashboard's Needs you card holds what it raises.
    "worker-image-missing-alone": Seed(lambda _owner: _short_release()),
    "policy-change": Seed(_policy_change, _SET_UP),
    "readings-aged": Seed(_readings_aged, _SET_UP),
    "glance-aged": Seed(_glance_aged, _SET_UP),
    "glance-asked": Seed(_glance_asked, _SET_UP),
    "glance-unanswered": Seed(_glance_unanswered, _SET_UP),
    "refreshes-asked": Seed(_refreshes_asked, _SET_UP),
    **{
        f"update-{ended}": Seed(_approved(ended), _SET_UP)
        for ended in ("queued", "preview-failed", "not-applied", "held", "applied")
    },
    # The open app update's new image, whose publisher attaches no package list.
    "update-unlisted": Seed(_unlisted, _SET_UP),
    "app-updates": Seed(_updates(), _SET_UP),
    **{f"app-update-{at}": Seed(_opening(at), _SET_UP) for at in ("asked", "running", "failed", "opened")},
    # The open app update's checks under way.
    "app-update-checks-running": Seed(_checks_running, _SET_UP),
    # Applied only by a playbook written for another machine.
    "app-updates-elsewhere": Seed(_updates("edge-01"), _SET_UP),
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
