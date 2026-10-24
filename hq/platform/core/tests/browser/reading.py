"""How the gates run probes on a page: a probe page of markup, and many probes in one trip."""

from functools import cache, wraps

from django.conf import settings
lazy from django.template import Context, Template

from hq.platform.core.tests.browser.serving import NONCE

# A probe page: a table drawn through the table tags, as a template draws one,
# with the stylesheet and the table script.
_PROBE_PAGE = """{% load table_tags stylesheet_tags %}<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% stylesheets %}
<script defer src="{{ static }}js/tables.js" nonce="{{ nonce }}"></script>
</head><body><main>"""


def probe(markup):
    """``markup``, in the table tags, as a page."""

    return Template(_PROBE_PAGE + markup + "</main></body></html>").render(
        Context({"static": settings.STATIC_URL, "nonce": NONCE})
    )


class ProbeThrew(Exception):
    """A probe threw in the page, so its rule could not be judged."""


@cache
def _in_one_trip(sources):
    """One function that runs each probe in turn, each on its own, and says
    what each returned or threw: ``[[true, value] | [false, error], ...]``.
    A round trip to the page costs milliseconds, a probe often less."""

    probes = ",\n".join(f"({source})" for source in sources)
    return f"""async (args) => {{
  const probes = [{probes}];
  const out = [];
  for (let at = 0; at < probes.length; at += 1) {{
    try {{ out.push([true, await probes[at](args[at])]); }}
    catch (error) {{ out.push([false, String(error)]); }}
  }}
  return out;
}}"""


def _probe(probe):
    return probe if isinstance(probe, tuple) else (probe, None)


def taken(outcome):
    """A probe's value, or what it threw raised as ProbeThrew."""

    returned, value = outcome
    if not returned:
        raise ProbeThrew(value)
    return value


def reads(*probes):
    """A rule that judges only what its probes return: a probe is its source,
    or its source and argument. ``hold`` reads the probes of a run of such
    rules in one round trip; a rule called alone reads its own."""

    probes = tuple(map(_probe, probes))

    def rule(judge):
        @wraps(judge)
        def held(self, name, *found):
            return judge(self, name, *(found or self.read(probes)))

        # typeshed's _Wrapped declares no attributes, which a function takes.
        held.probes = probes  # type: ignore[attr-defined]
        return held

    return rule
