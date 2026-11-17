"""The signature form ``template_contract`` writes a template tag's parameters in.

Kept apart from it only until the host-run modules of ``hq_sdk`` become private, when it moves there.
"""

import inspect
from annotationlib import Format
from typing import Any


def _parameters(target: Any) -> list[str] | None:
    """A signature as short strings: ``name``, ``name=`` when it has a default,
    ``*args``/``**kwargs``, and the bare ``*`` and ``/`` markers Python uses."""

    try:
        signature = inspect.signature(target, annotation_format=Format.STRING)
    except TypeError, ValueError:
        # A callable Python gives no signature (a builtin's) has none to write.
        return None
    parameters = list(signature.parameters.values())
    rendered: list[str] = []
    keyword_only_started = False
    for index, parameter in enumerate(parameters):
        kind = parameter.kind
        if kind is inspect.Parameter.KEYWORD_ONLY and not keyword_only_started:
            rendered.append("*")
            keyword_only_started = True
        if kind is inspect.Parameter.VAR_POSITIONAL:
            rendered.append(f"*{parameter.name}")
            keyword_only_started = True
            continue
        if kind is inspect.Parameter.VAR_KEYWORD:
            rendered.append(f"**{parameter.name}")
            continue
        optional = parameter.default is not inspect.Parameter.empty
        rendered.append(f"{parameter.name}=" if optional else parameter.name)
        following = parameters[index + 1] if index + 1 < len(parameters) else None
        if kind is inspect.Parameter.POSITIONAL_ONLY and (following is None or following.kind is not kind):
            rendered.append("/")
    return rendered
