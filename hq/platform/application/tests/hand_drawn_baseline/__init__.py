"""The host pages that still draw a shared part by hand, and how many times.

Each is a page to move onto the part ``HAND_DRAWN`` names for its shape, and
``test_hand_drawn`` fails when an entry's count is wrong either way: a new one
is refused, and a page moved must lower its entry here. So this only falls.
One module per wave of moves, so waves running at once never edit one file.
"""

from . import facts, forms, rows, sections

BASELINE: dict[tuple[str, str], int] = {**forms.BASELINE, **facts.BASELINE, **sections.BASELINE, **rows.BASELINE}
