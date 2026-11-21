"""The cells the infrastructure pages' tables share."""

from collections.abc import Iterable
from dataclasses import replace

from hq.platform.application.tables import Cell, Piece
lazy from hq.platform.core.included_parts import CHANGE


def run(values: Iterable[Cell]) -> Cell:
    """Values on one line, each after the one before it; nothing for none."""

    first, *rest = (*values,) or (Cell(),)
    return replace(first, parts=(*first.parts, *rest)) if rest else first


def codes(values: Iterable[str]) -> Cell:
    """Identifiers on one line, each as a readout draws it."""

    return run(Cell(str(value), readout=True) for value in values)


def change(was: str, now: str, *, url: str = "", code: bool = False) -> Cell:
    """A value and the one it changes into, as the change part draws them:
    identifiers with ``code``, the new one linked outside HQ with ``url``."""

    return Cell(piece=Piece(CHANGE, {"from": was, "to": now, "url": url, "code": code}, f"{was} to {now}"))
