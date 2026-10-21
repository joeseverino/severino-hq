"""Every list page, filled with the longest values its model can hold.

The dense estate is written by hand, so it holds only the values someone
thought of; a list nobody wrote rows for lays out on whatever the sparse estate
gives it. Here every view in the URLconf that draws its rows through
``TableListMixin`` gets a page, and its model's rows are derived from the
model's own fields: text at its ``max_length`` in words that must wrap with one
unbroken identifier among them, a paragraph for free text, an address as long
as the field takes, a number at the largest its field and validators allow,
and the longest label of every choice. A new list view is covered the day it is
routed, and a new field the day it is declared.

Every row passes the model's own ``full_clean`` (its validators and its
``Meta.constraints``), so the rows are ones HQ could store. Every value is
invented: names are under example.com.
"""

import re
from contextlib import ExitStack
from datetime import datetime, time
from decimal import Decimal
from itertools import product

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from django.utils.text import slugify

from hq.platform.application.references import ReferenceField
from hq.platform.application.tables import TableListMixin
from hq.platform.application.tests.markup import elements
from hq.platform.core.tests.test_browser_fixtures import render_pages

# Words a record really says, run to the field's length so a column must wrap them.
_WORDS = (
    "An example value written out to the full length its field allows so that every column it is shown in "
    "has to hold the longest one a record can carry and still keep inside the box it is drawn in"
)
_PARAGRAPH = (
    "An example note written the way an operator writes one: what was bought or done, why it was needed, "
    "what it replaced, and what to check the next time it comes up. It runs to several sentences, names an "
    "identifier such as {token} in passing, and keeps going long enough that a cell showing it must clamp "
    "or wrap it rather than widen its column to fit. A layout that holds this holds a real one."
)


def _token(index: int) -> str:
    """One unbroken run with no space in it, as long as an order number or a part number."""

    return f"example-{index:04d}-0123456789ab"


_DERIVED = re.compile(r"example-\d{4}-0123456789ab")


def _words(length: int, index: int) -> str:
    """Words to ``length``, the identifier among them, unique to the row."""

    words = _WORDS.split()
    said = " ".join((*words[:2], _token(index)))
    for word in words[2:] * 4:
        if len(said) + 1 + len(word) > length:
            break
        said += " " + word
    return said[:length]


def _url(length: int, index: int) -> str:
    said = f"https://an-example-site-with-a-long-name-{index}.example.com/"
    while len(said) < length:
        said += "an-example-page-with-a-long-path/"
    return said[:length].rstrip("/-")


# The largest amount one person's household records: a six-figure purchase.
LARGEST_AMOUNT = Decimal("999999.99")


def _largest(field: models.Field) -> int | Decimal:
    """The largest number the field stores and its validators admit; money no
    larger than LARGEST_AMOUNT."""

    limits = [validator.limit_value for validator in field.validators if isinstance(validator, MaxValueValidator)]
    if isinstance(field, models.DecimalField):
        whole = Decimal(10) ** (field.max_digits - field.decimal_places)
        limits += [whole - Decimal(1).scaleb(-field.decimal_places), LARGEST_AMOUNT]
    return min(limits)


def _longest_first(field: models.Field) -> list[str]:
    """The field's choices, the one with the longest label first."""

    return [value for value, label in sorted(field.flatchoices, key=lambda choice: -len(str(choice[1]))) if value != ""]


def _when() -> datetime:
    """A day whose date reads at its longest: two-digit, in the longest month."""

    today = timezone.localdate()
    return timezone.make_aware(datetime.combine(today.replace(year=today.year - 1, month=9, day=28), time(23, 59)))


def _value(field: models.Field, index: int):
    """The worst-case value for one field, or None to leave it at its default."""

    length = getattr(field, "max_length", None)
    if isinstance(field, models.URLField):
        return _url(length, index)
    if isinstance(field, models.EmailField):
        return f"an-example-address-with-a-long-name-{index}@example.com"
    if isinstance(field, models.SlugField):
        return slugify(_words(length, index))[:length].strip("-")
    if isinstance(field, models.FileField):
        name = f"example/{slugify(_words(length, index))}"
        return name[: length - len(".pdf")].rstrip("-") + ".pdf"
    if isinstance(field, models.CharField):
        return _words(length, index)
    if isinstance(field, models.TextField):
        return _PARAGRAPH.format(token=_token(index))
    if isinstance(field, (models.DecimalField, models.IntegerField)):
        return _largest(field)
    if isinstance(field, models.DateTimeField):
        return _when()
    if isinstance(field, models.DateField):
        return _when().date()
    return None


def _smaller(number: int | Decimal) -> int | Decimal:
    """The largest run of nines a digit shorter: 32767 to 9999, 9999.99 to 999.99."""

    nines = 10 ** (len(str(int(number))) - 1) - 1
    return nines + (number - int(number)) if isinstance(number, Decimal) else nines


def _admitted(model, values, exclude):
    """``values`` with each number its model's rules refuse made a digit shorter
    until they admit it: a rule can bound a number its field does not."""

    while True:
        try:
            model(**values).full_clean(exclude=exclude, validate_unique=False)
        except ValidationError as error:
            refused = {
                name: _smaller(values[name])
                for name in error.message_dict
                if isinstance(values.get(name), (int, Decimal)) and values[name] > 9
            }
            if not refused:
                raise
            values |= refused
        else:
            return values


def _filled(field: models.Field) -> bool:
    """Whether a row's value for ``field`` is derived here. Keys, relations and
    what HQ computes are not; a reference names a record, so it is left empty."""

    return (
        field.concrete
        and field.editable
        and not field.primary_key
        and not field.is_relation
        and not isinstance(field, ReferenceField)
        and not isinstance(field, (models.BooleanField, models.JSONField))
    )


class _Fill:
    """Rows for each model a list shows, and one for each model they relate to."""

    def __init__(self, user):
        self.user = user
        self.rows: dict[type[models.Model], list[models.Model]] = {}

    def rows_for(self, model: type[models.Model]) -> list[models.Model]:
        """Enough rows that every choice of every field is shown at least once."""

        if model not in self.rows:
            choices = [_longest_first(field) for field in model._meta.concrete_fields if field.choices]
            count = max([3, *(len(each) for each in choices)])
            self.rows[model] = [self._row(model, index) for index in range(count)]
            for row in self.rows[model]:
                self._relate(row)
        return self.rows[model]

    def _row(self, model, index):
        fields = [field for field in model._meta.concrete_fields if _filled(field)]
        chosen = [field for field in fields if field.choices]
        plain = _admitted(
            model,
            {field.name: value for field in fields if not field.choices if (value := _value(field, index)) is not None},
            exclude=[field.name for field in chosen],
        )
        # Row `index` starts each choice field `index` places down its longest-first list.
        ranked = []
        for field in chosen:
            values = _longest_first(field)
            ranked.append(values[index % len(values) :] + values[: index % len(values)])
        last = None
        for combination in product(*ranked):
            row = model(**plain, **{field.name: value for field, value in zip(chosen, combination, strict=True)})
            try:
                row.full_clean()
            except ValidationError as error:
                last = error
                continue
            row.save()
            return row
        raise AssertionError(f"no {model.__name__} row its own rules admit: {last}")

    def _relate(self, row):
        """Each optional relation pointed at a row of its own."""

        for field in row._meta.concrete_fields:
            if field.is_relation and field.null:
                setattr(row, field.name, self._target(field.related_model))
        row.full_clean()
        row.save()
        for field in row._meta.many_to_many:
            getattr(row, field.name).add(self._target(field.related_model))

    def _target(self, model):
        if model is get_user_model():
            return self.user
        return self.rows_for(model)[0]


def list_views():
    """Every routed view that draws its rows through TableListMixin, by URL name."""

    def walk(resolver, namespaces):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern, (*namespaces, pattern.namespace) if pattern.namespace else namespaces)
            elif isinstance(pattern, URLPattern):
                view = getattr(pattern.callback, "view_class", None)
                if view is not None and issubclass(view, TableListMixin):
                    if pattern.pattern.regex.groups:
                        raise AssertionError(f"{pattern.name} takes arguments a derived page cannot fill")
                    yield ":".join((*namespaces, pattern.name)), view

    return dict(walk(get_resolver(), ()))


def build_derived_estate():
    """Rows for every list, and the user their pages are rendered for."""

    user = get_user_model().objects.create_user(
        username="example-derived", password="not-a-real-password", first_name="Example"
    )
    fill = _Fill(user)
    for view in list_views().values():
        fill.rows_for(view.model)
    return user


def derived_pages():
    """One page per list, named under derived/ so a failure says which estate."""

    return {f"derived/{name.replace(':', '/')}": (lambda name=name: reverse(name), ExitStack) for name in list_views()}


def render_derived_pages(user):
    return render_pages(user, derived_pages())


class DerivedEstateTests(TestCase):
    def test_every_list_page_shows_the_rows_derived_for_it(self):
        """A list a derived row cannot be shown on would be measured empty."""

        for name, html in render_derived_pages(build_derived_estate()).items():
            with self.subTest(page=name):
                cells = [cell.words for cell in elements(html, "td")]
                self.assertTrue(any(_DERIVED.search(said) for said in cells), "no derived row is shown")
