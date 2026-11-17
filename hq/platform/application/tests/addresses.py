"""An address for any route, so a walk of the URL configuration reaches the ones that take an argument."""

import re

# A value each path converter takes.
SAMPLES = {
    "int": "1",
    "str": "example",
    "slug": "example",
    "path": "example",
    "uuid": "00000000-0000-4000-8000-000000000000",
}
PARAMETER = re.compile(r"<(?:(?P<converter>\w+):)?\w+>")


def address(route: str) -> str:
    """``route`` with each argument a value its converter takes."""

    def sample(match: re.Match[str]) -> str:
        converter = match["converter"] or "str"
        if converter not in SAMPLES:
            raise AssertionError(f"{route} takes a {converter!r}, which this walk has no sample of")
        return SAMPLES[converter]

    return PARAMETER.sub(sample, route)
