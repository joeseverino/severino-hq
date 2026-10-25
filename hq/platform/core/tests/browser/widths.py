"""The widths the gates read a page at, and the table regions the stylesheet draws at them."""

import re

from hq.platform.core.stylesheets import stylesheet

# The widths the gates read a page at, declared once. A set that reads more
# says why beside it; a test with a width of its own (a dashboard's pairs, a
# wide table on a desktop) names it where it is read.
#
# The phone HQ is read on: its viewport in CSS pixels.
PHONE_SCREEN = (402, 874)

# Where a frame across most of the page has the room to be judged empty.
WIDE = 1360

# A phone, a tablet held upright, a laptop: the screens every sweep reads.
WIDTHS = (PHONE_SCREEN[0], 820, WIDE)

# The narrowest phone a page must still fit.
NARROWEST = 320

# The sparse estate is read across the stylesheet's media queries as well: at
# the narrowest phone, a tablet's short side, and the widest window the
# `max-width: 1280px` rules hold, which WIDE is past.
SPARSE_WIDTHS = (NARROWEST, PHONE_SCREEN[0], 768, 1280)

# What a pressed control says is read where a head has the least room beside
# its title, on the phones, and where it has the most.
SPEAKING_WIDTHS = (NARROWEST, PHONE_SCREEN[0], WIDE)

# The least a control is hit through, either way, in CSS pixels (WCAG 2.5.8),
# which the stylesheet's `--target-min` is held to.
TARGET_MIN = 24


def _found(pattern, css, what):
    match = re.search(pattern, css)
    if not match:
        raise AssertionError(f"The stylesheet no longer says {what} the way the browser gate reads it ({pattern})")
    return match


def wide_tables(css, region):
    """The narrowest viewports whose table region is ``region`` wide."""

    page, card = first_viewports(css, region)
    # Two viewports this close stretch the same columns, so the wider holds both.
    return region, (card,) if card - page <= 50 else (page, card)


def first_viewports(css, region):
    """The narrowest viewports whose table region is ``region`` wide: the
    page's own region, inside the content's padding
    (`--content-pad: clamp(a, b vw, c)`) and the border of the surface that
    scrolls it, and one in a card, which adds its border and inline padding."""

    low, share, high = map(
        float, _found(r"--content-pad: clamp\((\d+)px, ([\d.]+)vw, (\d+)px\)", css, "the content's padding").groups()
    )
    space = _found(
        r"\.card \{ padding: var\(--surface-inset, var\(--space-\d+\) var\((--space-\d+)\)\)", css, "a card's padding"
    ).group(1)
    padding = float(_found(rf"{space}: (\d+)px;", css, "a card's inline padding").group(1))
    border = float(_found(r"border: var\(--surface-border, (\d+)px solid", css, "a surface's border").group(1))

    def first(inset):
        width = region
        while width - 2 * (min(max(low, share * width / 100), high) + inset) < region:
            width += 1
        return width

    return first(border), first(padding + border)


# A region this wide reads a name as never cut whole, and the laptop windows
# where a region has only just become so are read besides WIDTHS.
WIDE_REGION, WIDE_TABLES = wide_tables(stylesheet(), 1000)

# The stylesheet's phone breakpoint: `@media (max-width: 640px)`.
PHONE = 640


def viewport(width):
    return {"width": width, "height": PHONE_SCREEN[1] if width <= PHONE else 900}
