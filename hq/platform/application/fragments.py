"""A page, or one named part of it, from one definition.

A template marks a part with Django's ``{% partialdef name inline %}``. A
request that names the part in ``X-Fragment`` is answered with the part alone,
rendered from the same template and the same context as the page, so the two
cannot drift. A request that names a part the template does not define is
answered with the page: the browser finds the region in it by its id, which is
what it would have done with no header at all.

    class CalendarView(PageMixin, TemplateView):   # PageMixin is a FragmentMixin
        template_name = "calendars/calendar.html"

    return fragments.render(request, "core/_dashboard_glance.html", context)

``static/js/fragment.js`` is the other half: the element that says which part
it is and where it comes from.

**Answering "unchanged".** A part whose inputs are known by their revisions
answers a repeated question with 304 and composes nothing. `standing` is the
validator: what the answer was derived from, then the second it stops holding.
The header's count and a polled part share it.
"""

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponseNotModified
from django.template import loader
from django.utils.cache import patch_vary_headers

HEADER = "X-Fragment"

# A partial's name is a template identifier; anything else names no part.
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# A validator: what the answer was derived from, then the second it stops holding.
_VALIDATOR = re.compile(r'^"([0-9a-f]{64})-(\d{1,12})"$')

# An answer with no moment of its own is asked about again within the day.
LONGEST = 24 * 60 * 60

# Markup carries ages a template prints ("5 minutes ago") and belongs to the
# build that rendered it, so a part vouches for itself for a minute at most
# and only to the process that rendered it.
PART_HOLDS = 60
_PROCESS = str(time.time_ns())


def requested(request: HttpRequest) -> str:
    """The part this request asks for by name; empty when it asks for the page."""

    name = request.headers.get(HEADER, "")
    return name if _NAME.fullmatch(name) else ""


def template_names(request: HttpRequest, names: list[str]) -> list[str]:
    """``names`` with the requested part of each tried first, then the page."""

    part = requested(request)
    return [f"{name}#{part}" for name in names] + list(names) if part else list(names)


def base(request: HttpRequest, *inputs: object) -> str | None:
    """What an answer is derived from, as one key; None when an input is not known.

    The reader is always part of it: no answer vouches for another person's.
    An unknown input is never read as "unchanged".
    """

    if any(value is None for value in inputs):
        return None
    parts = "|".join(str(value) for value in (*inputs, request.user.pk))
    return hashlib.sha256(parts.encode()).hexdigest()


def presented(request: HttpRequest, derived_from: str | None) -> str | None:
    """The validator the request presented, if it still vouches for the answer.

    It does when the answer would be derived from the same inputs now and the
    moment it stops holding has not come. Anything else is None and the answer
    is composed.
    """

    found = _VALIDATOR.match(request.headers.get("If-None-Match", ""))
    if found is None or derived_from is None or found[1] != derived_from:
        return None
    if time.time() >= int(found[2]):
        return None
    return found[0]


def standing(derived_from: str | None, until: datetime | None = None, *, longest: int = LONGEST) -> str | None:
    """The validator for an answer just composed from ``derived_from``."""

    if derived_from is None:
        return None
    limit = int(time.time()) + longest
    return f'"{derived_from}-{min(limit, int(until.timestamp())) if until else limit}"'


def hold(response: HttpResponse, validator: str | None) -> HttpResponse:
    """Let the browser keep ``response`` and ask about it before every reuse."""

    if validator:
        response.headers["ETag"] = validator
        response.headers["Cache-Control"] = "private, no-cache"
    return response


def render(
    request: HttpRequest,
    template_name: str,
    context: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
    *,
    status: int = 200,
    revision: object = None,
) -> HttpResponse:
    """The page ``template_name`` draws, or the part of it the request names.

    ``context`` may be a callable, composed only when the answer is. With a
    ``revision`` (whatever moves when the answer would), a request presenting
    the validator of an answer that still stands is answered 304 from the
    revision alone.
    """

    part = requested(request)
    derived_from = None
    if revision is not None and request.method in ("GET", "HEAD"):
        derived_from = base(request, revision, request.get_full_path(), part, _PROCESS)
    still = presented(request, derived_from)
    if still:
        response: HttpResponse = HttpResponseNotModified()
        response.headers["ETag"] = still
    else:
        template = loader.select_template(template_names(request, [template_name]))
        body = template.render(context() if callable(context) else context, request)
        response = hold(HttpResponse(body, status=status), standing(derived_from, longest=PART_HOLDS))
    patch_vary_headers(response, (HEADER,))
    return response


class FragmentMixin:
    """A template view that answers the page, or the part of it a request names."""

    def get_template_names(self) -> list[str]:
        return template_names(self.request, super().get_template_names())

    def render_to_response(self, context: Mapping[str, Any], **kwargs: Any) -> HttpResponse:
        response = super().render_to_response(context, **kwargs)
        # The same address answers two documents; a cache must not confuse them.
        patch_vary_headers(response, (HEADER,))
        return response
