"""The commands a page offers, as its Copy button gives them."""

import re
from html import unescape

_BLOCK = re.compile(r'<pre class="operator-command"><code>(.*?)</code></pre>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def commands_on(response) -> str:
    """Every command block's text, one per line: markup that only places a
    line break (``<wbr>``, a kept-whole span) is not part of the command."""

    body = response.content.decode() if hasattr(response, "content") else str(response)
    return "\n".join(unescape(_TAG.sub("", block)) for block in _BLOCK.findall(body))
