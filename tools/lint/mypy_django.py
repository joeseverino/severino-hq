"""django-stubs' mypy plugin, with its cache keyed on the dependencies it adds.

The plugin's ``report_config_data`` does not cover the models modules it makes
others depend on, so a warm cache reports a gone one missing. It can go once
django-stubs' own covers them.
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
            file._fullname = module
            dependencies[module] = sorted(name for _, name, _ in self.get_additional_deps(file))
        return dict(sorted(dependencies.items()))

    @override
    def report_config_data(self, ctx: ReportConfigContext) -> dict[str, Any]:
        return {**super().report_config_data(ctx), "model_dependencies": self._model_dependencies}


def plugin(version: str) -> type[Plugin]:
    return DjangoPlugin
