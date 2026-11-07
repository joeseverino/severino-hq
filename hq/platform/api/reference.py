"""The machine API's OpenAPI document, rendered for the signed-in operator.

A session page, not an API endpoint: it is mounted beside /api/ (the domain
registry's ``hq.api``) and keeps the login redirect every page has.
"""

from typing import cast

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.csp import CSP
from django.views.decorators.csp import csp_override
from django.views.decorators.http import require_safe

from hq.config.devtools import without_trusted_types
from hq.platform.api.openapi import document

# The one page served under a policy of its own, derived from the real one so
# tightening that cannot leave a stale copy here. It differs in two ways:
# - Trusted Types are dropped: the vendored Scalar bundle (static/vendor/scalar)
#   writes strings into innerHTML through Vue and its markdown renderer, and no
#   configuration of it creates only the `hq-fragment` policy.
# - Style elements are admitted inline, with no nonce: CodeMirror inside the
#   bundle writes a <style> that takes a nonce only from `EditorView.cspNonce`,
#   which Scalar never sets and offers no option for, and a nonce in the
#   directive would make browsers ignore 'unsafe-inline'. The page shows only
#   HQ's own registry, so no style here is anyone else's.
# It needs no inline script and, configured jitless, no eval.
_APPLICATION = cast("dict[str, list[str]]", settings.SECURE_CSP)
POLICY = without_trusted_types(_APPLICATION) | {"style-src-elem": [*_APPLICATION["style-src-elem"], CSP.UNSAFE_INLINE]}


@require_safe
@csp_override(POLICY)
def reference(request: HttpRequest) -> HttpResponse:
    # The longest path the document serves, in characters: every list of a
    # tag's operations is that wide, so their edges line up (api-reference.css).
    longest = max(map(len, document()["paths"]), default=0)
    return TemplateResponse(
        request, "hq_api/reference.html", {"document_url": reverse("hq_api:openapi"), "longest_path": longest}
    )
