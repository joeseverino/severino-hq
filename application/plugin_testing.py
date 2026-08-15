"""Boot an extension the way production does: alongside a sibling.

A plugin's own suite loads that plugin and nothing else, so a surface that
composes across extensions is only ever tested in a world where it is alone.
Every assertion that reads "nothing else is installed" then passes locally and
is wrong in production, and no per-repo CI can see it — the sibling lives in a
different repository.

The failure this exists to prevent, verbatim: a composing page asserted its
empty state and an empty cross-extension queue. Both held when it was the only
extension loaded. Installed beside a real sibling, the page had a panel and the
queue had entries, and the tests that should have caught it were the ones
asserting the opposite.

    class HomeTests(ComposedPluginTestCase, TestCase):
        siblings = (sibling(cards=({"id": "a", "label": "Open", "value": 3,
                                    "url": "/a/"},)),)

        def test_a_sibling_panel_appears(self):
            ...

Siblings are synthetic: no import, no database, no second repository checked
out. They exist to make "something else is installed" true.
"""

from __future__ import annotations

from contextlib import ExitStack
import os
from typing import Any, Iterable
from unittest import mock

from .plugins import PluginManifest, installed_plugins
from .ui import Insight

# Deliberately not a name any real extension would take. A synthetic sibling
# that collided with a real id would silently displace it.
SIBLING_PREFIX = "example."


def sibling(
    *,
    identifier: str = "example.alpha",
    name: str = "Alpha",
    cards: Iterable[dict[str, Any]] = (),
    attention: Iterable[Insight] = (),
    **manifest_fields: Any,
) -> tuple[PluginManifest, tuple[dict[str, Any], ...], tuple[Insight, ...]]:
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
        # Set only when the sibling actually reports something: the host reads
        # these by importing the reference, and an empty one is never imported.
        dashboard_provider=f"{identifier}:cards" if cards else "",
        attention_provider=f"{identifier}:attention" if attention else "",
        **manifest_fields,
    )
    return manifest, tuple(cards), tuple(attention)


class ComposedPluginTestCase:
    """Mixin: installs `siblings` beside whatever the suite already loads.

    Mix in before the Django test case. Declare `siblings` as a class attribute
    holding the result of `sibling(...)` calls.
    """

    siblings: tuple = ()

    def setUp(self):  # noqa: N802 - unittest's own name
        super().setUp()
        self._composition = ExitStack()
        self.addCleanup(self._composition.close)
        # The registry is cached for the process; a sibling appearing or leaving
        # mid-suite would otherwise be invisible or permanent.
        installed_plugins.cache_clear()
        self.addCleanup(installed_plugins.cache_clear)

        real = os.environ.get("SEVERINO_HQ_PLUGINS", "")
        manifests = [manifest for manifest, _, _ in self.siblings]
        contributions = {
            manifest.id: (cards, attention)
            for manifest, cards, attention in self.siblings
        }
        references = [f"{manifest.id}:manifest" for manifest in manifests]
        self._composition.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "SEVERINO_HQ_PLUGINS": ",".join(
                        part for part in (real, *references) if part
                    )
                },
                clear=False,
            )
        )

        original = __import__("application.plugins", fromlist=["_import"])._import

        def _import(spec: str):
            """Resolve a synthetic reference; defer to the real one otherwise.

            Deferring matters: the plugin under test is loaded by its real
            module path, and replacing the importer outright would unload it.
            """
            module, _, attribute = spec.partition(":")
            if module in contributions:
                cards, attention = contributions[module]
                if attribute == "manifest":
                    return next(m for m in manifests if m.id == module)
                if attribute == "cards":
                    return lambda: cards
                if attribute == "attention":
                    return lambda: attention
            return original(spec)

        self._composition.enter_context(
            mock.patch("application.plugins._import", side_effect=_import)
        )
