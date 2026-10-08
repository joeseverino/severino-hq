"""Boot an extension the way production does: alongside a sibling.

A plugin's own suite loads that plugin and nothing else, so a surface that
composes across extensions is only ever tested in a world where it is alone.
Every assertion that reads "nothing else is installed" then passes locally and
is wrong in production, and no per-repo CI can see it: the sibling lives in a
different repository.

A composing page that asserts an empty state or an empty cross-extension queue
must hold that assertion with a sibling installed, because production always
has one.

    class HomeTests(ComposedPluginTestCase, TestCase):
        siblings = (sibling(cards=({"id": "a", "label": "Open", "value": 3,
                                    "url": "/a/"},)),)

        def test_a_sibling_panel_appears(self):
            ...

Siblings are synthetic: no import, no database, no second repository checked
out. They exist to make "something else is installed" true.
"""

import os
import re
from collections.abc import Iterable
from contextlib import ExitStack
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock

from .plugins import (
    PLUGIN_API_VERSION,
    PluginIntegration,
    PluginManifest,
    clear_plugin_composition_cache,
)
from .ui import Insight

# Deliberately not a name any real extension would take. A synthetic sibling
# that collided with a real id would silently displace it.
SIBLING_PREFIX = "example."


def undefined_style_classes(template_root) -> list[str]:
    """Class names an extension's templates use that the host does not define.

    An extension that invents a class gets no error and no styling: the page
    renders, slightly wrong, and stays that way. The host has this check for
    its own partials, but it cannot see an extension's templates: they live in
    another repository and are not installed when the host's suite runs. So the
    check has to run from the extension's side, against the host's real bundle.

        class StyleTests(SimpleTestCase):
            def test_templates_only_use_defined_classes(self):
                root = Path(__file__).resolve().parent / "templates"
                self.assertEqual(undefined_style_classes(root), [])

    Returns sorted "template.html: .name" strings so a failure names the file.
    """
    css = Path(__file__).resolve().parents[3] / "static" / "css" / "app.css"
    defined = set(re.findall(r"\.([a-z][a-z0-9-]*)", css.read_text(encoding="utf-8")))
    offenders = set()
    for template in sorted(Path(template_root).rglob("*.html")):
        text = template.read_text(encoding="utf-8")
        for attribute in re.findall(r'class="([^"]*)"', text):
            # Interpolated values are decided at render time; the pieces that
            # make them up are checked where they are defined instead.
            if "{{" in attribute or "{%" in attribute:
                continue
            for name in attribute.split():
                if name not in defined:
                    offenders.add(f"{template.name}: .{name}")
    return sorted(offenders)


def sibling(
    *,
    identifier: str = "example.alpha",
    name: str = "Alpha",
    cards: Iterable[dict[str, Any]] = (),
    attention: Iterable[Insight] = (),
    overview: Any = None,
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
    """Each provider of ``plugin_id`` that is not answered from what is stored.

    HQ asks a plugin's attention, dashboard, overview and calendar providers
    through a derivation, and answers a second question from the first's
    stored answer until a table the provider read is written. A provider is
    named here when that does not hold: what it returned could not be kept (it
    does not pickle), it comes back unequal to what was stored, or it is
    built anew on every call around something that cannot be compared.

        def test_every_provider_is_answered_from_what_is_stored(self):
            self.assertEqual(providers_derived_again("example.notes"), [])

    Call it with the extension's records in place, so the providers have
    something to say. ``window`` is the days the calendar sources are asked
    for; the year around today when left out.
    """

    from django.utils import timezone

    from .derivations import counting
    from .plugins import DERIVED_PROVIDERS, installed_integrations
    from .projection import projection_scope

    first, last = window or (
        timezone.localdate() - timedelta(days=183),
        timezone.localdate() + timedelta(days=183),
    )

    def questions() -> dict[str, Any]:
        integration = next((found for plugin, found in installed_integrations() if plugin.id == plugin_id), None)
        if integration is None:
            raise LookupError(f"No installed plugin has the id {plugin_id!r}.")
        asked = {
            field: provider for field in DERIVED_PROVIDERS if (provider := getattr(integration, field)) is not None
        }
        for source in integration.calendars() if integration.calendars else ():
            asked[f"calendar {source.id}"] = partial(source.events, first, last)
        return asked

    with projection_scope():
        stored = {name: ask() for name, ask in questions().items()}
    again: list[str] = []
    for name, ask in questions().items():
        with projection_scope(), counting() as (ran, served):
            answer = ask()
        if ran or not served:
            again.append(f"{name}: derived again, its answer was not kept")
        elif answer != stored[name]:
            again.append(f"{name}: the stored answer is not equal to the one derived")
    return again


class ComposedPluginTestCase:
    """Mixin: installs `siblings` beside whatever the suite already loads.

    Mix in before the Django test case. Declare `siblings` as a class attribute
    holding the result of `sibling(...)` calls.
    """

    siblings: tuple = ()

    def setUp(self):
        super().setUp()
        self._composition = ExitStack()
        self.addCleanup(self._composition.close)
        # The registry is cached for the process; a sibling appearing or leaving
        # mid-suite would otherwise be invisible or permanent.
        clear_plugin_composition_cache()
        self.addCleanup(clear_plugin_composition_cache)

        real = os.environ.get("SEVERINO_HQ_PLUGINS", "")
        manifests = [manifest for manifest, _ in self.siblings]
        contributions = {manifest.id: integration for manifest, integration in self.siblings}
        references = [f"{manifest.id}:manifest" for manifest in manifests]
        self._composition.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "SEVERINO_HQ_PLUGINS": ",".join(part for part in (real, *references) if part),
                    # A sibling built here exists for the length of one test.
                    # It has no wheel, no artifact digest and no signed
                    # approval, so it can never appear in the admission lock,
                    # and admission requires the lock to match the enabled set
                    # exactly. Left on, the kit's own siblings are read as an
                    # unsigned plugin and every suite using it fails.
                    #
                    # Stated rather than inherited, because admission defaults
                    # to off under DEBUG and on otherwise, and the composed
                    # image runs this suite with DEBUG off. A test kit must
                    # behave the same in both.
                    "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
                },
                clear=False,
            )
        )

        original = __import__("hq.platform.application.plugins", fromlist=["_import"])._import

        def _import(spec: str):
            """Resolve a synthetic reference; defer to the real one otherwise.

            Deferring matters: the plugin under test is loaded by its real
            module path, and replacing the importer outright would unload it.
            """
            module, _, attribute = spec.partition(":")
            if module in contributions:
                if attribute == "manifest":
                    return next(m for m in manifests if m.id == module)
                if attribute == "integration":
                    return lambda: contributions[module]
            return original(spec)

        self._composition.enter_context(mock.patch("hq.platform.application.plugins._import", side_effect=_import))
