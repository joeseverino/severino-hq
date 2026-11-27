"""A piece of work said as the steps it goes through, in order: each step
stands as asked-for work does (``asks.Standing``), so a step done, under way,
failed or waiting reads and colors the same on every card that lists one."""

from dataclasses import dataclass

from .asks import DONE, Standing
from .entity_links import EntityLink, declared_link


@dataclass(frozen=True, slots=True)
class Step:
    """One step: its title, how it stands (its word, its sentence and the
    page that holds it), the parts it is made of (each check, each app), and
    who took it, where a person did."""

    title: str
    standing: Standing
    parts: tuple[EntityLink | str, ...] = ()
    by: str = ""

    @property
    def done(self) -> bool:
        return self.standing.state == DONE

    @property
    def link(self) -> EntityLink | None:
        """Its title as a link to the page that holds it, None where none does."""

        return declared_link(self.title, self.standing.url) if self.standing.url else None
