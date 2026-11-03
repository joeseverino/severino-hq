"""Everything a booted HQ has registered, as sorted names, for a boot to be compared with another.

`python -m hq.platform.core.tests.registries asgi lazy` boots HQ as its server
does and prints one JSON line, `manage lazy` as manage.py does, `test lazy`
as manage.py does once a test overrides a setting; `eager` in
place of `lazy` boots it with imports as written, only an explicit ``lazy`` one
waiting (some break an import cycle, so nothing boots with none). Under lazy imports a registration that only an import's side
effect made is missing, which test_lazy_imports.py reads as a difference.
"""

import gc
import json
import sys
import weakref
from typing import Any

PREFIX = "registries "


def _named(thing: Any) -> str:
    """``module:qualified.name``, a bound method by its class."""

    owner = getattr(thing, "__self__", None)
    if owner is not None:
        return f"{_named(type(owner))}.{getattr(thing, '__name__', '?')}"
    return f"{getattr(thing, '__module__', '?')}:{getattr(thing, '__qualname__', repr(thing))}"


def module_of(name: str) -> str:
    """The module a name ``_named`` gave is defined in."""

    return name.partition(":")[0]


def _receivers(signals=None) -> list[str]:
    """Each live receiver of ``signals`` (every signal by default), with the
    model it listens to, by name."""

    from django.apps import apps
    from django.dispatch import Signal

    senders = {id(model): model._meta.label for model in apps.get_models()} | {id(None): "any sender"}
    found: list[str] = []
    for signal in signals or (thing for thing in gc.get_objects() if isinstance(thing, Signal)):
        for (_receiver, sender), reference, *_ in signal.receivers:
            receiver = reference() if isinstance(reference, weakref.ref) else reference
            found.append(f"{_named(receiver)} from {senders.get(sender, 'a sender')}")
    return found


def _lookups() -> dict[str, list[str]]:
    """Each lookup and transform registered on every class that takes them, by class."""

    from django.db.models.query_utils import RegisterLookupMixin

    classes: list[type] = []
    pending: list[type] = [RegisterLookupMixin]
    while pending:
        cls = pending.pop()
        classes.append(cls)
        pending += cls.__subclasses__()
    return {_named(cls): sorted(cls.get_class_lookups()) for cls in classes}


def _templates() -> list[str]:
    from django.template import engines

    engine = engines["django"].engine
    found = [f"builtin {name}" for library in engine.template_builtins for name in (*library.tags, *library.filters)]
    for label, module in engine.template_libraries.items():
        found += [f"{label} {name}" for name in (*module.tags, *module.filters)]
    return found


def registries() -> dict[str, Any]:
    """What a boot left registered: Django's and HQ's, each as sorted names."""

    from django.apps import apps
    from django.core.checks.registry import registry as checks
    from django.core.signals import setting_changed

    from hq.platform.application.domains import domain_navigation
    from hq.platform.application.integrations import integration_graph
    from hq.platform.application.plugins import installed_plugin_apps
    from hq.platform.application.routes import routed
    from hq.platform.core.audit import audited_labels

    # What the boot itself left, read before anything below loads a module of its own.
    booted = {
        "loaded": sorted(sys.modules),
        # The packages whose registrations are HQ's to make explicit: the host's and each extension's.
        "ours": sorted({"hq", "hq_sdk", *(app.partition(".")[0] for app in installed_plugin_apps())}),
        "models": sorted(model._meta.label for model in apps.get_models()),
        "receivers": sorted(_receivers()),
        # What overriding a setting connected to clear the reads of it cached
        # elsewhere, held whatever loaded: the receiver's module serves state
        # in others, so it is missed even where it never loaded.
        "overrides": sorted(set(_receivers([setting_changed])) - set(_BEFORE_OVERRIDE["receivers"]))
        if _BEFORE_OVERRIDE
        else [],
        "lookups": _lookups(),
        "checks": sorted(_named(check) for check in checks.get_checks(include_deployment_checks=True)),
    }
    graph = integration_graph()
    return booted | {
        "urls": sorted(f"{view.name} {view.path} {_named(view.pattern.callback)}" for view in routed()),
        "templates": sorted(_templates()),
        "audited": sorted(label for label in audited_labels().values()),
        "capabilities": sorted(graph.capabilities),
        "resources": sorted(graph.resources),
        "connections": sorted(graph.connections),
        "search": sorted(graph.search),
        "navigation": sorted(f"{entry.route}" for entry in domain_navigation()),
    }


ENTRYPOINTS = ("asgi", "manage", "test")

# The receivers of ``setting_changed`` before the test entrypoint overrides a setting.
_BEFORE_OVERRIDE: dict[str, list[str]] = {}


def boot(entrypoint: str, mode: str) -> None:
    """HQ as one of its entrypoints starts it: the server's ASGI application,
    manage.py, or manage.py once a test has overridden a setting.

    Each reaches a library's modules in its own order, and builds its own
    objects (a storage connects a receiver as it is made), so a lazy boot is
    compared with an eager one through the same entrypoint. Django's test
    tools connect the receivers that reset what reads a setting only when
    ``override_settings`` first loads, which no start reaches.
    """

    from hq.config import imports

    if mode == "eager":
        imports.eagerly()
    if entrypoint in ("manage", "test"):
        import manage

        sys.argv = ["manage.py", "version"]
        manage.main()
        if entrypoint == "test":
            from django.core.signals import setting_changed

            # Read before django.test is imported, which an eager boot loads whole.
            _BEFORE_OVERRIDE["receivers"] = _receivers([setting_changed])
            from django.test import override_settings

            with override_settings():
                pass
    else:
        import hq.config.asgi  # noqa: F401 - the server's own start, which is what is measured


if __name__ == "__main__":
    boot(*sys.argv[1:3])
    print(PREFIX + json.dumps(registries()))
