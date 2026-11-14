"""The code patch: a block of code to paste somewhere else, with what to know
about it.

``{% patch id="hardening" %}services: ...{% title %}Add to <code>compose.yaml</code>{% detail %}Then run it.{% notes %}<li>A note</li>{% endpatch %}``
draws ``partials/_patch.html``: the title beside a Copy button, the code in a
``<pre>`` the button copies, the words under it, and the notes list. With no
code it draws the notes alone.
"""

from django import template

from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

PATCH_PARTIAL = "partials/_patch.html"


slotted("patch", PATCH_PARTIAL, ("content", "title", "detail", "notes"), frozenset(("id",)), library=register)
