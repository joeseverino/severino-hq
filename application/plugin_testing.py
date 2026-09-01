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
from pathlib import Path
import re
from typing import Any, Iterable
from unittest import mock

from .plugins import PluginIntegration, PluginManifest, installed_plugins
from .ui import Insight

# Deliberately not a name any real extension would take. A synthetic sibling
# that collided with a real id would silently displace it.
SIBLING_PREFIX = "example."


def undefined_style_classes(template_root) -> list[str]:
    """Class names an extension's templates use that the host does not define.

    An extension that invents a class gets no error and no styling -- the page
    renders, slightly wrong, and stays that way. The host has this check for
    its own partials, but it cannot see an extension's templates: they live in
    another repository and are not installed when the host's suite runs. So the
    check has to run from the extension's side, against the host's real bundle.

    It caught `.section-action` -- a section-head link two extensions used and
    nothing ever styled, shipped to production reading as a plain browser link.

        class StyleTests(SimpleTestCase):
            def test_templates_only_use_defined_classes(self):
                root = Path(__file__).resolve().parent / "templates"
                self.assertEqual(undefined_style_classes(root), [])

    Returns sorted "template.html: .name" strings so a failure names the file.
    """
    import application

    css = Path(application.__file__).resolve().parents[1] / "static" / "css" / "app.css"
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
        manifests = [manifest for manifest, _ in self.siblings]
        contributions = {
            manifest.id: integration for manifest, integration in self.siblings
        }
        references = [f"{manifest.id}:manifest" for manifest in manifests]
        self._composition.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "SEVERINO_HQ_PLUGINS": ",".join(
                        part for part in (real, *references) if part
                    ),
                    # A sibling built here exists for the length of one test.
                    # It has no wheel, no artifact digest and no signed
                    # approval, so it can never appear in the admission lock --
                    # and admission requires the lock to match the enabled set
                    # exactly. Left on, the kit's own siblings are read as an
                    # unsigned plugin and every suite using it fails.
                    #
                    # Stated rather than inherited, because admission defaults
                    # to off under DEBUG and on otherwise. Both the local gate
                    # and CI run with DEBUG on, so this passed everywhere it
                    # was run and failed in the one place it was not: the
                    # composed image, which runs this suite with DEBUG off.
                    # A test kit must not behave differently there.
                    "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
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
                if attribute == "manifest":
                    return next(m for m in manifests if m.id == module)
                if attribute == "integration":
                    return lambda: contributions[module]
            return original(spec)

        self._composition.enter_context(
            mock.patch("application.plugins._import", side_effect=_import)
        )
