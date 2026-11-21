"""Boot an extension the way production does: alongside a sibling.

A plugin's own suite loads that plugin alone, so an assertion that nothing else
is installed (an empty state, an empty cross-extension queue) passes there and
is wrong in production, where a sibling always is, and no per-repo CI can see
it. ``ComposedPluginTestCase`` installs synthetic siblings (``sibling``): no
import, no database, no second repository, only "something else is installed".
"""

import os
import sys
import types
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any
from unittest import mock

lazy from django.utils import timezone

from .finding_model import Finding
from .plugins import (
    DERIVED_PROVIDERS,
    PLUGIN_API_VERSION,
    PluginIntegration,
    PluginManifest,
)
from .ui import DomainOverview
lazy from . import pages, plugins
lazy from .connection_contracts import ConnectionSpec
lazy from .derivation_testing import frozen
lazy from .derivations import UNSEEN, counting
lazy from .projection import projection_scope, read_once
lazy from .security import WEB_INTERFACE, Principal
lazy from .web_writes import RETRY_KEY_FIELD, new_key

# Deliberately not a name any real extension would take. A synthetic sibling
# that collided with a real id would silently displace it.
SIBLING_PREFIX = "example."


def sibling(
    *,
    identifier: str = "example.alpha",
    name: str = "Alpha",
    cards: Iterable[dict[str, Any]] = (),
    attention: Iterable[Finding] = (),
    overview: DomainOverview | None = None,
    **manifest_fields: Any,
) -> tuple[PluginManifest, PluginIntegration]:
    """One synthetic extension: a manifest plus what it reports.

    Returns the manifest and its contributions together so the caller declares a
    sibling in one expression rather than wiring a provider by module path that
    would have to exist on disk.
    """
    if not identifier.startswith(SIBLING_PREFIX):
        raise ValueError(
            f"A synthetic sibling id must start with {SIBLING_PREFIX!r} so it "
            f"cannot displace a real extension; got {identifier!r}."
        )
    manifest = PluginManifest(
        id=identifier,
        name=name,
        version="0.0.0",
        distribution=identifier.replace(".", "-"),
        source_repository=f"example/{identifier.replace('.', '-')}",
        source_workflow=".github/workflows/admit-plugin.yml",
        api_version=PLUGIN_API_VERSION,
        integration_provider=f"{identifier}:integration",
        **manifest_fields,
    )
    card_values = tuple(cards)
    attention_values = tuple(attention)
    integration = PluginIntegration(
        dashboard=(lambda: card_values) if card_values else None,
        attention=(lambda: attention_values) if attention_values else None,
        overview=(lambda: overview) if overview is not None else None,
    )
    return manifest, integration


def providers_derived_again(plugin_id: str, *, window: tuple[date, date] | None = None) -> list[str]:
    """Each provider of ``plugin_id`` that is not answered from what is stored,
    or whose answer is not what its kind must be.

    Named when what it returned does not pickle, comes back unequal to what was
    stored, is built anew around something that cannot be compared, or stands
    a minute because it reads the instant (docs/Plugins.md#derived-reads),
    and when a page would show it as
    unreadable: an attention item, card or overview the host refuses. Call it
    with the extension's records in
    place; ``window`` is the days calendar sources are asked for, the year
    around today when left out.
    """

    first, last = window or (
        timezone.localdate() - timedelta(days=183),
        timezone.localdate() + timedelta(days=183),
    )

    def questions() -> dict[str, Any]:
        integration = next(
            (found for plugin, found in plugins.installed_integrations() if plugin.id == plugin_id), None
        )
        if integration is None:
            raise LookupError(f"No installed plugin has the id {plugin_id!r}.")
        asked = {
            field: provider for field in DERIVED_PROVIDERS if (provider := getattr(integration, field)) is not None
        }
        for source in integration.calendars() if integration.calendars else ():
            asked[f"calendar {source.id}"] = partial(source.events, first, last)
        return asked

    # Held at noon, so an answer that lapses with the clock is not mistaken
    # for one that was never kept, nor a day's for an instant's.
    with frozen(timezone.make_aware(datetime.combine(timezone.localdate(), time(12)))) as noon:
        stored, again = {}, []
        for name, ask in questions().items():
            with projection_scope():
                stored[name] = ask()
                held = read_once("derivation.answered", dict).values()
            if any(until and until <= noon + UNSEEN for until in held):
                again.append(f"{name}: derived again every minute, it reads the instant; read the day")
        again += _refused(plugin_id, stored)
        for name, ask in questions().items():
            with projection_scope(), counting() as (ran, served):
                answer = ask()
            if ran or not served:
                again.append(f"{name}: derived again, its answer was not kept")
            elif answer != stored[name]:
                again.append(f"{name}: the stored answer is not equal to the one derived")
    return again


def _refused(plugin_id: str, answers: Mapping[str, Any]) -> list[str]:
    """Each of ``plugin_id``'s answers, by provider, that the host refuses, with why."""

    refused = []
    for name, answer in answers.items():
        check = plugins.SOURCE_CHECKS.get(name)
        try:
            if check is not None:
                check(plugin_id, answer)
        except plugins.ImproperlyConfigured as refusal:
            refused.append(f"{name}: {refusal}")
    return refused


@contextmanager
def importable(modules: Mapping[str, Mapping[str, Any]]) -> Iterator[None]:
    """Each module named in ``modules`` importable for the block, holding the
    attributes given for it.

    A synthetic extension is then reached the way production reaches a real
    one, by the ``module:attribute`` references the host imports, and a name
    no module here holds imports as it always would.
    """

    made = {}
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        vars(module).update(attributes)
        made[name] = module
    # Only these names are put back: a module first imported inside the block
    # stays imported, as it would after any other import.
    held = {name: sys.modules[name] for name in made if name in sys.modules}
    sys.modules.update(made)
    try:
        yield
    finally:
        for name, module in made.items():
            if sys.modules.get(name) is module:
                del sys.modules[name]
        sys.modules.update(held)


@contextmanager
def installed(*siblings: tuple[PluginManifest, PluginIntegration], alone: bool = False) -> Iterator[None]:
    """``siblings`` installed beside whatever the suite already loads, for the
    block: each the result of a ``sibling(...)`` call. With ``alone`` they are
    the whole installed set, so ``installed(alone=True)`` is the host as it runs
    with no extension."""

    real = "" if alone else os.environ.get("SEVERINO_HQ_PLUGINS", "")
    references = [f"{manifest.id}:manifest" for manifest, _ in siblings]
    environment = {
        "SEVERINO_HQ_PLUGINS": ",".join(part for part in (real, *references) if part),
        # A sibling built here exists for the length of one test. It has no
        # wheel, no artifact digest and no signed approval, so it can never
        # appear in the admission lock, and admission requires the lock to
        # match the enabled set exactly. Left on, the kit's own siblings are
        # read as an unsigned plugin and every suite using it fails.
        #
        # Stated rather than inherited, because admission defaults to off
        # under DEBUG and on otherwise, and the composed image runs this suite
        # with DEBUG off. A test kit must behave the same in both.
        "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
    }
    modules = {
        manifest.id: {"manifest": manifest, "integration": lambda found=integration: found}
        for manifest, integration in siblings
    }
    with importable(modules), mock.patch.dict(os.environ, environment, clear=False):
        yield


def offering(*specs: ConnectionSpec, alone: bool = False, **answers: tuple[Any, ...]) -> AbstractContextManager[None]:
    """An example extension installed for the block, offering ``specs`` as its
    connection families and answering each field of ``answers`` with its value;
    with ``alone``, the only extension installed."""

    manifest, contribution = sibling(identifier="example.connections", name="Connections")
    provided = {field: (lambda found=found: found) for field, found in {"connections": specs, **answers}.items()}
    return installed((manifest, replace(contribution, **provided)), alone=alone)


class ComposedPluginTestCase:
    """Mixin: installs `siblings` beside whatever the suite already loads.

    Mix in before the Django test case. Declare `siblings` as a class attribute
    holding the result of `sibling(...)` calls.
    """

    siblings: tuple = ()

    def setUp(self) -> None:
        super().setUp()
        self._composition = ExitStack()
        self.addCleanup(self._composition.close)
        self._composition.enter_context(installed(*self.siblings))


def form_keyed(data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``data`` with the retry key a write form posts, drawn as ``{% write_token %}``
    draws one, for a test that posts a form without opening its page first."""

    return {**(data or {}), RETRY_KEY_FIELD: new_key()}


def holding(*capabilities: str) -> AbstractContextManager[Any]:
    """While open, a signed-in person holds only ``capabilities`` at the page gate.

    Patches the one name the gate reads a person's principal through, so a test
    of which pages open asks the real gate, whatever role the person has.
    """

    def principal(user: Any) -> Principal:
        return Principal(user.get_username(), WEB_INTERFACE, frozenset(capabilities))

    return mock.patch.object(pages, "web_principal", principal)
