"""The commands a page offers, as its Copy button gives them."""

from .markup import with_class


def commands_on(response) -> str:
    """Every command block's text, one per line: markup that only places a
    line break (``<wbr>``, a kept-whole span) is not part of the command."""

    return "\n".join(block.text for block in with_class(response, "operator-command"))
