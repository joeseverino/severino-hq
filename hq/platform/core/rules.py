"""A row-level rule, declared once on the model.

``Rule`` is a check constraint that also says which field its message belongs
beside. From the one declaration in ``Meta.constraints``:

- the database refuses a row that breaks it, whatever wrote the row: a form,
  an importer, ``QuerySet.update()``, a bulk write or a shell;
- ``full_clean`` reports it before the write, beside that field, so a form
  shows it where the person typed and a command names the field.

A rule holds for one row on its own: it compares the row's columns with each
other or with constants. A rule about other rows is a unique constraint or a
query. A bound on a single number that a form control should also carry is
field validators with a check constraint behind them
(``hq.platform.application.business_use``).

Adding a rule to a model with rows is a migration that fails if a row breaks
it. ``manage.py constraint_preflight`` counts those rows first, reading only.
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models


class Rule(models.CheckConstraint):
    """A check constraint whose violation is reported beside ``field``."""

    def __init__(self, *, field: str = "", **kwargs: Any) -> None:
        if not kwargs.get("violation_error_message"):
            raise ValueError("A rule says what it requires in violation_error_message.")
        super().__init__(**kwargs)
        self.field = field

    def validate(self, model, instance, exclude=None, using=DEFAULT_DB_ALIAS) -> None:
        try:
            super().validate(model, instance, exclude=exclude, using=using)
        except ValidationError as error:
            # A form that does not carry the field shows the message on its own.
            if not self.field or (exclude and self.field in exclude):
                raise
            raise ValidationError({self.field: error.error_list}) from None

    def deconstruct(self):
        path, args, kwargs = super().deconstruct()
        if self.field:
            kwargs["field"] = self.field
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Rule):
            return self.field == other.field and super().__eq__(other)
        return super().__eq__(other)


def one_of(field: str, choices: type[models.Choices], name: str) -> Rule:
    """The rule that ``field`` holds one of the values ``choices`` declares."""

    return Rule(
        condition=models.Q(**{f"{field}__in": choices.values}),
        name=name,
        violation_error_message=f"Must be one of: {', '.join(map(str, choices.values))}.",
        field=field,
    )


def singleton(name: str) -> Rule:
    """The rule that a model holds one row, at primary key 1."""

    return Rule(
        condition=models.Q(id=1),
        name=name,
        violation_error_message="There is one of these, with id 1.",
    )
