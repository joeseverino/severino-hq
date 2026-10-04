"""The machine API's OpenAPI document, rendered for the signed-in operator.

A session page, not an API endpoint: it is mounted beside /api/ (the domain
registry's ``hq.api``) and keeps the login redirect every page has.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.csp import csp_override
from django.views.decorators.http import require_safe


@require_safe
@csp_override(settings.SEVERINO_API_REFERENCE_CSP)
def reference(request: HttpRequest) -> HttpResponse:
    return TemplateResponse(
        request, "hq_api/reference.html", {"document_url": reverse("hq_api:openapi")}
    )
