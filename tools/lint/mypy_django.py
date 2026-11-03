"""django-stubs' mypy plugin, with its cache keyed on the dependencies it adds.

The plugin makes a module that imports ``django.apps`` depend on every models
module, and a models module on each module its relations reach. Its
``report_config_data``, which mypy compares to decide whether a module's cache
still holds, covers ``INSTALLED_APPS`` but not those dependencies, so a warm
cache keeps one on a models module that is gone and reports it missing where a
cold run passes. This adds the dependencies themselves to that data. It can go
once django-stubs' own ``report_config_data`` covers the models modules.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any, override

from mypy.nodes import MypyFile
from mypy.plugin import Plugin, ReportConfigContext
from mypy_django_plugin.main import NewSemanalDjangoPlugin


class DjangoPlugin(NewSemanalDjangoPlugin):
    @cached_property
    def _model_dependencies(self) -> dict[str, list[str]]:
        # Asked of the plugin itself, so the key is whatever it would add.
        dependencies = {}
        for module in self.django_context.model_modules:
            file = MypyFile([], [])
            file._fullname = module  # noqa: SLF001 - MypyFile has no setter, and the plugin reads only the name
            dependencies[module] = sorted(name for _, name, _ in self.get_additional_deps(file))
        return dict(sorted(dependencies.items()))

    @override
    def report_config_data(self, ctx: ReportConfigContext) -> dict[str, Any]:
        return {**super().report_config_data(ctx), "model_dependencies": self._model_dependencies}


def plugin(version: str) -> type[Plugin]:  # noqa: ARG001 - mypy's entry point passes its version
    return DjangoPlugin
