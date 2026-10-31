"""Boot an extension the way production does: alongside a sibling.

A plugin's own suite loads that plugin alone, so an assertion that nothing else
is installed (an empty state, an empty cross-extension queue) passes there and
is wrong in production, where a sibling always is, and no per-repo CI can see
it. ``ComposedPluginTestCase`` installs synthetic siblings (``sibling``): no
import, no database, no second repository, only "something else is installed".
"""

import os
import re
import sys
import types
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import replace
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock

lazy from django.template import engines
lazy from django.template.base import TextNode, Variable
lazy from django.template.defaulttags import IfNode
lazy from django.template.library import TagHelperNode
lazy from django.utils import timezone

lazy from hq.platform.core.stylesheets import stylesheet
lazy from hq.platform.core.templatetags import table_tags

from .plugins import (
    DERIVED_PROVIDERS,
    PLUGIN_API_VERSION,
    PluginIntegration,
    PluginManifest,
    clear_plugin_composition_cache,
)
from .ui import DomainOverview, Insight
lazy from . import plugins
lazy from .connection_contracts import ConnectionSpec
lazy from .derivation_testing import frozen
lazy from .derivations import counting
lazy from .projection import projection_scope

# Deliberately not a name any real extension would take. A synthetic sibling
# that collided with a real id would silently displace it.
SIBLING_PREFIX = "example."


def stylesheet_selectors(css: str) -> list[str]:
    """Every selector of a stylesheet, each rule's comma-separated list split.

    Only text ahead of a ``{`` is read, so a number, a ``url()`` or a custom
    property value never passes for a class."""

    rules = re.findall(r"([^{};]+)\{", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    return [
        selector.strip()
        for rule in rules
        for selector in re.sub(r"\[[^\]]*\]|url\([^)]*\)|\"[^\"]*\"|'[^']*'", "", rule).split(",")
        if selector.strip()
    ]


def selector_classes(selector: str) -> list[str]:
    """The class names a selector writes, in order."""

    return re.findall(r"\.([a-zA-Z_][\w-]*)", selector)


def undefined_style_classes(template_root: str | Path) -> list[str]:
    """Class names an extension's templates use that the host does not define.

    An invented class renders unstyled with no error, and the host's own check
    cannot see templates in another repository, so this runs from the
    extension's suite against the host's real bundle, as
    ``assertEqual(undefined_style_classes(templates), [])``. Returns sorted
    "template.html: .name" strings so a failure names the file.
    """
    defined = {name for selector in stylesheet_selectors(stylesheet()) for name in selector_classes(selector)}
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


_OPEN, _CLOSE = "\x00region\x00", "\x00/region\x00"
_HEADING, _DROPPED_HEADING = "\x00heading\x00", "\x00heading-drops\x00"
_TABLE = re.compile(r'<table\b[^>]*\bclass="[^"]*\bdata-table\b')
_HEAD = re.compile(r"<thead\b.*?</thead>", re.DOTALL)
_HAND_HEADING = re.compile(r"<th[\s>]")


def _heading_marker(node) -> str:
    """A heading, marked when it is given a literal drop rank."""

    drop = node.kwargs.get("drop")
    literal = drop.var.literal if drop is not None and isinstance(drop.var, Variable) else None
    return _DROPPED_HEADING if literal else _HEADING


def _flatten(nodelist, out: list[str]) -> list[str]:
    """The template's markup in source order, every branch included, with the
    table tags standing as markers."""

    for node in nodelist:
        if isinstance(node, TextNode):
            out.append(node.s)
        elif isinstance(node, table_tags.RegionNode):
            out.append(_OPEN)
            _flatten(node.nodelist, out)
            out.append(_CLOSE)
        elif isinstance(node, TagHelperNode) and node.func is table_tags.table_heading:
            out.append(_HEADING if "select_all" in node.kwargs else _heading_marker(node))
        elif isinstance(node, IfNode):
            for _condition, branch in node.conditions_nodelists:
                _flatten(branch, out)
        else:
            for attribute in node.child_nodelists:
                _flatten(getattr(node, attribute, None) or (), out)
    return out


def table_problems(source: str) -> list[str]:
    """What keeps a template's tables off the contract: a table outside its region,
    a heading written by hand, a region written by hand, a dropped name column."""

    markup = "".join(_flatten(engines["django"].from_string(source).template.nodelist, []))
    problems = []
    if re.search(r'class="[^"]*\btable-scroll\b', markup):
        problems.append("a scroll region written by hand")
    for table in _TABLE.finditer(markup):
        before = markup[: table.start()]
        if before.count(_OPEN) <= before.count(_CLOSE):
            problems.append("a data table outside {% table_region %}")
        body = markup[table.start() : markup.find("</table>", table.start())]
        for head in _HEAD.findall(body):
            if _HAND_HEADING.search(head):
                problems.append("a heading not drawn by {% table_heading %}")
            first = re.search(f"{_HEADING}|{_DROPPED_HEADING}", head)
            if first and first.group() == _DROPPED_HEADING:
                problems.append("a drop rank on the column that names each row")
    return problems


def hand_drawn_tables(template_root: str | Path) -> list[str]:
    """Tables an extension's templates draw by hand, which the host's script
    neither sorts nor fits: every table is declared through ``{% table_region %}``
    and ``{% table_heading %}``. Run from the extension's side like
    ``undefined_style_classes``; returns "template.html: problem" strings."""

    return [
        f"{template.name}: {problem}"
        for template in sorted(Path(template_root).rglob("*.html"))
        for problem in table_problems(template.read_text(encoding="utf-8"))
    ]


def sibling(
    *,
    identifier: str = "example.alpha",
    name: str = "Alpha",
    cards: Iterable[dict[str, Any]] = (),
    attention: Iterable[Insight] = (),
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
    """Each provider of ``plugin_id`` that is not answered from what is stored.

    Named when what it returned does not pickle, comes back unequal to what was
    stored, or is built anew around something that cannot be compared
    (docs/Plugins.md#derived-reads). Call it with the extension's records in
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

    # Held, so an answer that lapses with the clock is not mistaken for one
    # that was never kept.
    with frozen():
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
    # The registry is cached for the process; a sibling appearing or leaving
    # mid-suite would otherwise be invisible or permanent.
    clear_plugin_composition_cache()
    try:
        with importable(modules), mock.patch.dict(os.environ, environment, clear=False):
            yield
    finally:
        clear_plugin_composition_cache()


def offering(*specs: ConnectionSpec, alone: bool = False) -> AbstractContextManager[None]:
    """An example extension installed for the block, offering ``specs`` as its
    connection families; with ``alone``, the only extension installed."""

    manifest, contribution = sibling(identifier="example.connections", name="Connections")
    return installed((manifest, replace(contribution, connections=lambda: specs)), alone=alone)


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
