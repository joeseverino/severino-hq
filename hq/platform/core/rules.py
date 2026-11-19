"""A row-level rule, declared once on the model.

``Rule`` is a check constraint that also says which field its message belongs
beside: the database refuses a row that breaks it, whatever wrote the row, and
``full_clean`` reports it before the write, beside that field. A rule compares
one row's columns with each other or with constants; one about other rows is a
unique constraint or a query.
"""

from typing import Any, override

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models
from django.db.models.functions import Cast, Length, Trim
from django.db.models.lookups import GreaterThan

# The code a rule's refusal carries, so a refusal may say the rule's own
# message: declared here, it holds nothing a caller sent.
RULE = "rule"


class Rule(models.CheckConstraint):  # noqa: PLW1641 - unhashable, as Django's CheckConstraint is, since both compare by value
    """A check constraint whose violation is reported beside ``field``.

    ``pattern``, for a rule about one text column, is what a value matches when
    the row keeps the rule, so the command that writes the column refuses the
    rest (``stored_columns.stored_rules``).
    """

    def __init__(self, *, field: str = "", pattern: str = "", **kwargs: Any) -> None:
        if not kwargs.get("violation_error_message"):
            raise ValueError("A rule says what it requires in violation_error_message.")
        kwargs.setdefault("violation_error_code", RULE)
        super().__init__(**kwargs)
        self.field = field
        self.pattern = pattern

    @override
    def validate(self, model, instance, exclude=None, using=DEFAULT_DB_ALIAS) -> None:
        try:
            super().validate(model, instance, exclude=exclude, using=using)
        except ValidationError as error:
            # A form that does not carry the field shows the message on its own.
            if not self.field or (exclude and self.field in exclude):
                raise
            raise ValidationError({self.field: error.error_list}) from None

    @override
    def deconstruct(self):
        path, args, kwargs = super().deconstruct()
        # Every rule's code, so no migration states it.
        if kwargs.get("violation_error_code") == RULE:
            del kwargs["violation_error_code"]
        if self.field:
            kwargs["field"] = self.field
        if self.pattern:
            kwargs["pattern"] = self.pattern
        return path, args, kwargs

    @override
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Rule):
            return self.field == other.field and self.pattern == other.pattern and super().__eq__(other)
        return super().__eq__(other)


def one_of(field: str, choices: type[models.Choices], name: str) -> Rule:
    """The rule that ``field`` holds one of the values ``choices`` declares."""

    return Rule(
        condition=models.Q(**{f"{field}__in": choices.values}),
        name=name,
        violation_error_message=f"Must be one of: {', '.join(map(str, choices.values))}.",
        field=field,
    )


def has_text(field: str, name: str, message: str) -> Rule:
    """The rule that the text ``field`` holds a character besides spaces."""

    # Counted in bytes, since SQLite counts a text's characters only up to its first NUL.
    return Rule(
        condition=models.Q(GreaterThan(Length(Cast(Trim(field), models.BinaryField())), 0)),
        name=name,
        violation_error_message=message,
        field=field,
        pattern="[^ ]",
    )


def singleton(name: str) -> Rule:
    """The rule that a model holds one row, at primary key 1."""

    return Rule(
        condition=models.Q(id=1),
        name=name,
        violation_error_message="There is one of these, with id 1.",
    )
