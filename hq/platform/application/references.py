"""One way for a record to name a thing in HQ, and for that thing to list what names it.

A reference is the text ``kind:identity``, the two things ``entity_links`` turns
into a name and a page (docs/Plugins.md#references is the contract). Nothing
here imports a model: the fields are found on the installed models, so an
extension's records refer to the host's, and the host's to an extension's,
with neither naming the other.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, override

from django import forms
from django.apps import apps
from django.core import checks
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Value
from django.db.models.functions import Cast

lazy from hq.domains.control_plane.providers import PROVIDERS

lazy from . import entity_links, security
lazy from .demo import showing_demo
lazy from .entity_links import NODE_KINDS, entity_link, kind_label, node_name
lazy from .projection import read_once
lazy from .security import Principal, cli_principal
lazy from .topology import derive_topology
lazy from .ui import Insight

# Named for annotations alone: a class's annotations can be read as it is made
# (a dataclass's are), which would load a lazy import at once.
if TYPE_CHECKING:
    from .entity_links import EntityLink

# The kinds the topology names by their label, which is what their page is addressed by.
ESTATE_KINDS = ("machine", "zone", "service")
NAME_SUFFIX = "_name"
# In a field's ``kinds``: every registry kind that is a certificate.
CERTIFICATE = "certificate"


@dataclass(frozen=True, slots=True)
class Reference:
    """``kind:identity``, taken apart."""

    kind: str
    identity: str

    @classmethod
    def parse(cls, value: Any) -> Reference | None:
        kind, found, identity = str(value or "").strip().partition(":")
        kind, identity = kind.strip(), identity.strip()
        return cls(kind, identity) if found and kind and identity else None

    def __str__(self) -> str:
        return f"{self.kind}:{self.identity}"


@dataclass(frozen=True, slots=True)
class Referable:
    """Declared on a model as ``referable``: its rows can be referred to."""

    # The link builder's kind for a record it already names; the model's own
    # label otherwise.
    kind: str = ""
    # The field its page is addressed by.
    key: str = "pk"
    # The columns its name and page are built from.
    shows: tuple[str, ...] = ()
    # The attribute that says the name; ``str(row)`` when blank.
    name: str = ""
    noun: str = ""
    plural: str = ""
    # What a field asks for, rather than a kind, when any model may fill it.
    role: str = ""
    # The capability a viewer needs before the row is named to them.
    requires: str = ""
    # Offered in a form's list. A kind with thousands of rows is referred to by hand.
    pickable: bool = True


@dataclass(frozen=True, slots=True)
class Target:
    """One referable model, as the resolver reads it."""

    kind: str
    model: type[models.Model]
    declared: Referable

    @property
    def noun(self) -> str:
        return self.declared.noun or str(self.model._meta.verbose_name)

    @property
    def plural(self) -> str:
        return self.declared.plural or str(self.model._meta.verbose_name_plural)

    @property
    def columns(self) -> tuple[str, ...]:
        return _unique((self.declared.key, *self.declared.shows))

    def name_of(self, row: models.Model) -> str:
        return str(getattr(row, self.declared.name) if self.declared.name else row)


class ReferenceField(models.CharField):
    """A column holding one reference, or "" for none.

    Its arguments, and the ``_name`` column beside it that keeps what the thing
    was called, are docs/Plugins.md#references. ``clean`` fills that name and
    refuses a new reference that names nothing; one already stored is kept
    when its thing goes.
    """

    def __init__(
        self,
        *args: Any,
        kinds: Sequence[str] = (),
        role: str = "",
        but: Sequence[str] = (),
        heading: str = "",
        shows: Sequence[str] = (),
        note: str = "",
        **kwargs: Any,
    ) -> None:
        self.kinds = tuple(kinds)
        self.role = role
        self.but = tuple(but)
        self.heading = heading
        self.shows = tuple(shows)
        self.note = note
        kwargs.setdefault("max_length", 300)
        kwargs.setdefault("blank", True)
        kwargs.setdefault("default", "")
        kwargs.setdefault("db_index", True)
        super().__init__(*args, **kwargs)

    @override
    def deconstruct(self) -> Any:
        """A plain text column to a migration: what it may name is not schema."""

        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.CharField", args, kwargs

    @property
    def name_attname(self) -> str:
        return f"{self.attname}{NAME_SUFFIX}"

    @override
    def check(self, **kwargs: Any) -> list[checks.CheckMessage]:
        found = list(super().check(**kwargs))
        names = {field.name for field in self.model._meta.get_fields()}
        missing = [name for name in (self.name_attname, *self.shows) if name not in names and name != "pk"]
        if missing:
            found.append(
                checks.Error(
                    f"A reference needs the column {', '.join(missing)} on its model.",
                    obj=self,
                    id="hq.references.E001",
                )
            )
        if not self.heading:
            found.append(
                checks.Error(
                    "A reference says what its rows are listed under (heading).",
                    obj=self,
                    id="hq.references.E002",
                )
            )
        return found

    def accepts(self, kind: str) -> bool:
        if self.kinds:
            return kind in self.kinds or (CERTIFICATE in self.kinds and kind in _certificate_kinds())
        if self.role:
            target = targets().get(kind)
            return target is not None and target.declared.role == self.role
        return kind not in self.but

    @override
    def clean(self, value: Any, model_instance: models.Model | None) -> Any:
        value = super().clean(value, model_instance)
        if model_instance is None:
            return value
        if not value:
            return value

        principal = _reader()
        reference = Reference.parse(value)
        link = _found([reference], principal=principal).get(reference) if reference else None
        if link is not None and self.accepts(reference.kind):
            setattr(model_instance, self.name_attname, link.label[: _name_length(self)])
            return value
        if not self._stored(model_instance, value):
            raise ValidationError(f"HQ has nothing called {value!r} to link this to.")
        return value

    @override
    def pre_save(self, model_instance: models.Model, add: bool) -> Any:
        """No reference keeps no name. ``clean`` is not asked about an empty value."""

        value = super().pre_save(model_instance, add)
        if not value:
            setattr(model_instance, self.name_attname, "")
        return value

    def _stored(self, row: models.Model, value: str) -> bool:
        """Whether the row already holds this reference, so its thing went after it was saved."""

        if row._state.adding or row.pk is None:
            return False
        return type(row)._default_manager.filter(pk=row.pk, **{self.attname: value}).exists()

    @override
    def formfield(self, **kwargs: Any) -> Any:
        return ReferenceChoiceField(
            reference=self,
            label=kwargs.get("label") or str(self.verbose_name).capitalize(),
            help_text=kwargs.get("help_text", self.help_text),
        )


def _reader() -> Principal:
    """Who reads when no viewer does: the reader the queue derives the estate
    for, so one stored topology answers both."""

    return cli_principal()


def _name_length(field: ReferenceField) -> int:
    column = field.model._meta.get_field(field.name_attname)
    return int(column.max_length or 200)


def _unique(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


# ----- What can be referred to ---------------------------------------------------


@cache
def targets() -> Mapping[str, Target]:
    """Every model that declares ``referable``, by its kind."""

    found: dict[str, Target] = {}
    for model in apps.get_models():
        declared = model.__dict__.get("referable")
        if not isinstance(declared, Referable):
            continue
        kind = declared.kind or model._meta.label_lower
        if kind in found:
            raise ValueError(f"Two models answer to the reference name {kind!r}.")
        found[kind] = Target(kind, model, declared)
    return found


@cache
def reference_fields() -> tuple[ReferenceField, ...]:
    """Every reference column on every installed model."""

    return tuple(
        field
        for model in apps.get_models()
        for field in model._meta.concrete_fields
        if isinstance(field, ReferenceField)
    )


def _estate(principal: Principal) -> Mapping[str, Mapping[str, str]]:
    """Every infrastructure thing by kind and identity, with its name.

    Read off the topology, which is derived once and kept until what it reads
    is written. A tracked record's kind is its registry kind and its identity
    its key, which is what ``entity_link`` takes for one.
    """

    def read() -> Mapping[str, Mapping[str, str]]:
        found: dict[str, dict[str, str]] = defaultdict(dict)
        for node in derive_topology(principal=principal).nodes:
            if node.kind in ESTATE_KINDS:
                found[node.kind][node.label] = node_name(node)
            elif node.kind == "resource" and node.kind_key:
                found[node.kind_key][node.label] = node_name(node)
        return found

    return read_once("references.estate", read)


def _certificate_kinds() -> tuple[str, ...]:
    return tuple(kind for kind, provider in PROVIDERS.items() if provider.covers)


def _may_name(target: Target, principal: Principal) -> bool:
    return not target.declared.requires or principal.permits(target.declared.requires)


def _text(column: str) -> Cast:
    return Cast(column, output_field=models.CharField())


def _rows(
    parts: Sequence[tuple[type[models.Model], tuple[str, ...], Q]],
) -> list[list[models.Model]]:
    """For each part, the rows of its model that match, in one statement.

    A row holds only the columns its part names. Every column crosses as text,
    so parts of different models and shapes are one UNION, and each value is
    read back by its own field.
    """

    if not parts:
        return []
    width = max(len(columns) for _model, columns, _where in parts)
    queries = [
        model._default_manager.filter(where)
        .order_by()
        .values_list(
            Value(index, output_field=models.IntegerField()),
            *(_text(column) for column in columns),
            *(Value("", output_field=models.CharField()) for _pad in range(width - len(columns))),
        )
        for index, (model, columns, where) in enumerate(parts)
    ]
    # The union would take the first model's own ordering, which names a column it does not select.
    combined = queries[0].union(*queries[1:], all=True).order_by() if len(queries) > 1 else queries[0]
    found: list[list[models.Model]] = [[] for _ in parts]
    for index, *values in combined:
        model, columns, _where = parts[index]
        found[index].append(_hydrate(model, columns, values))
    return found


def _hydrate(model: type[models.Model], columns: tuple[str, ...], values: Sequence[Any]) -> models.Model:
    held = {}
    for column, value in zip(columns, values, strict=False):
        field = model._meta.pk if column == "pk" else model._meta.get_field(column)
        held[column] = field.to_python(value) if value is not None else None
    return model(**held)


def _found(references: Iterable[Reference], *, principal: Principal) -> dict[Reference, EntityLink]:
    """The link for each reference that names something this principal may see.

    One read of the topology for every infrastructure kind, and one statement
    for every model kind together.
    """

    wanted = set(references)
    found: dict[Reference, EntityLink] = {}
    by_kind: dict[str, set[str]] = defaultdict(set)
    for reference in wanted:
        by_kind[reference.kind].add(reference.identity)
    known = targets()
    kinds = [kind for kind in by_kind if kind in known and _may_name(known[kind], principal)]
    parts = [
        (
            known[kind].model,
            known[kind].columns,
            Q(**{f"{known[kind].declared.key}__in": _keys(known[kind], by_kind[kind])}),
        )
        for kind in kinds
    ]
    for kind, rows in zip(kinds, _rows(parts), strict=True):
        target = known[kind]
        for row in rows:
            identity = str(getattr(row, target.declared.key))
            # A record the link builder already names keeps its one page and noun.
            found[Reference(kind, identity)] = (
                entity_link(kind, identity, label=target.name_of(row))
                if kind in NODE_KINDS
                else entity_links.EntityLink(
                    label=target.name_of(row),
                    url=row.get_absolute_url(),
                    kind=kind,
                    kind_label=target.noun.capitalize(),
                    identity=identity,
                )
            )
    if any(kind not in known for kind in by_kind):
        estate = _estate(principal)
        for reference in wanted:
            name = estate.get(reference.kind, {}).get(reference.identity)
            if name is not None:
                found[reference] = entity_link(reference.kind, reference.identity, label=name)
    return found


def _keys(target: Target, identities: Iterable[str]) -> list[Any]:
    """The identities that could be a key of this model; one that could not names nothing."""

    key = target.declared.key
    field = target.model._meta.pk if key == "pk" else target.model._meta.get_field(key)
    found = []
    for identity in identities:
        try:
            found.append(field.to_python(identity))
        except ValidationError:
            continue
    return found


def _noun(kind: str) -> str:
    target = targets().get(kind)
    return target.noun.capitalize() if target is not None else kind_label(kind)


def resolve_many(stored: Sequence[tuple[str, str]], *, principal: Principal) -> list[EntityLink | None]:
    """A link for each ``(reference, stored name)``, in order.

    None for no reference, and for one of a kind this principal may not be
    told about. A reference that names nothing answers with its stored name
    and no page.
    """

    parsed = [Reference.parse(value) for value, _name in stored]
    found = _found([reference for reference in parsed if reference], principal=principal)
    known = targets()
    links: list[EntityLink | None] = []
    for reference, (value, name) in zip(parsed, stored, strict=True):
        if reference is None:
            links.append(entity_links.EntityLink(label=name or str(value), kind_label="Not in HQ") if value else None)
        elif reference.kind in known and not _may_name(known[reference.kind], principal):
            links.append(None)
        elif reference in found:
            links.append(found[reference])
        else:
            noun = _safe_noun(reference.kind)
            # A stored name is the real one, which a demonstration does not show.
            label = noun if showing_demo() else (name or reference.identity)
            links.append(entity_links.EntityLink(label=label, kind=reference.kind, kind_label=noun))
    return links


def _safe_noun(kind: str) -> str:
    try:
        return _noun(kind)
    except KeyError:
        return "Not in HQ"


def resolve(value: str, name: str = "", *, principal: Principal) -> EntityLink | None:
    """One reference's link. See ``resolve_many``."""

    return resolve_many([(value, name)], principal=principal)[0]


def reference_of(row: models.Model, field: str, *, principal: Principal) -> EntityLink | None:
    """The link for the reference a row holds in ``field``."""

    return resolve(getattr(row, field, ""), getattr(row, f"{field}{NAME_SUFFIX}", ""), principal=principal)


def stored_name(value: str, *, principal: Principal) -> str:
    """What to keep beside a reference written without a form: its thing's
    name, or its identity where it names nothing yet."""

    reference = Reference.parse(value)
    if reference is None:
        return ""
    link = _found([reference], principal=principal).get(reference)
    return link.label if link is not None else reference.identity


def authored(value: object, *, principal: Principal) -> str:
    """A reference as a person wrote it, as the one HQ stores.

    A person names a kind by its noun: ``domain:example.com`` for a zone, and
    ``certificate:<key>`` for whichever certificate kind holds that key. Text
    that reads as no reference is kept as written, so a page can show it and
    the queue can report it.
    """

    reference = Reference.parse(value)
    if reference is None:
        return str(value or "").strip()
    nouns = {kind.noun: name for name, kind in NODE_KINDS.items() if kind.page is not None}
    nouns.update({target.noun: kind for kind, target in targets().items()})
    kind = nouns.get(reference.kind.lower(), reference.kind)
    if kind == "certificate":
        estate = _estate(principal)
        held = [name for name in _certificate_kinds() if reference.identity in estate.get(name, {})]
        kind = held[0] if held else kind
    return str(Reference(kind, reference.identity))


def as_stored(value: object) -> tuple[str, str]:
    """``(reference, name)`` to store for one a person wrote outside HQ.

    For a sync that copies records in: it has no viewer, and what it copies
    was not chosen from a list, so a reference that names nothing is stored
    all the same and reported by ``dangling``.
    """

    principal = _reader()
    reference = authored(value, principal=principal)
    return reference, stored_name(reference, principal=principal) or reference


# ----- Choosing one in a form ----------------------------------------------------


class ReferenceChoiceField(forms.ChoiceField):
    """The one picker for a reference: a list grouped by what each thing is.

    It offers nothing until ``bind`` gives it the viewer, so a form built
    without one accepts only the value it already holds.
    """

    def __init__(self, *, reference: ReferenceField, **kwargs: Any) -> None:
        self.reference = reference
        super().__init__(required=False, choices=[("", "Nothing")], **kwargs)


def bind(form: forms.BaseForm, *, principal: Principal) -> None:
    """Fill every reference picker on ``form`` with what this viewer may choose."""

    pickers = [name for name, field in form.fields.items() if isinstance(field, ReferenceChoiceField)]
    for name in pickers:
        field = form.fields[name]
        held = form.initial.get(name) or getattr(form.instance, name, "")
        kept = getattr(form.instance, f"{name}{NAME_SUFFIX}", "")
        field.choices = choices(field.reference, principal=principal, held=(held, kept))
    leave_off_unoffered(form, pickers)


def leave_off_unoffered(form: forms.BaseForm, pickers: Iterable[str]) -> None:
    """Drop each of ``pickers`` that offers nothing to choose from ``form``.

    The one rule for a reference picker on any form, the record's own or a
    command's: an empty list reads as a broken field. One the form cannot be
    sent without stays, so its emptiness is said rather than hidden.
    """

    for name in [name for name in pickers if not form.fields[name].required]:
        if not any(_option_values(form.fields[name].choices)):
            del form.fields[name]


class ReferencePickerMixin:
    """For a view of a form with a reference on it: its pickers offer what this viewer may choose."""

    def get_form(self, form_class: type[forms.BaseForm] | None = None) -> forms.BaseForm:
        form = super().get_form(form_class)
        bind(form, principal=security.web_principal(self.request.user))
        return form


def choices(field: ReferenceField, *, principal: Principal, held: tuple[str, str] = ("", "")) -> list[tuple[Any, Any]]:
    """The grouped options for one reference column.

    Infrastructure comes from the topology and every model kind from one
    statement. The value the row holds stays in the list when its thing has
    gone, so saving the form does not drop it.
    """

    offered = [
        (heading, options)
        for heading, options in (*_estate_groups(field, principal), *_record_groups(field, principal))
        if options
    ]
    return keeping_held([("", "Nothing"), *offered], [held])


NO_LONGER_OFFERED = "No longer in HQ"


def keeping_held(options: Sequence[tuple[Any, Any]], held: Iterable[tuple[str, str]]) -> list[tuple[Any, Any]]:
    """``options`` with each held ``(value, name)`` still among them.

    A value the list no longer offers is added under a heading of its own, so
    a form shows what the record holds and saving it unchanged keeps it rather
    than dropping it or quietly choosing another.
    """

    offered = set(_option_values(options))
    missing: dict[str, str] = {}
    for value, name in held:
        if value and value not in offered:
            missing.setdefault(value, name or value)
    if not missing:
        return list(options)
    return [*options, (NO_LONGER_OFFERED, list(missing.items()))]


def _option_values(options: Iterable[tuple[Any, Any]]) -> Iterable[Any]:
    """Every value in a choices list, whether flat or grouped."""

    for value, label in options:
        if isinstance(label, (list, tuple)):
            yield from (option for option, _name in label)
        else:
            yield value


type _Group = tuple[str, list[tuple[str, str]]]


def _estate_groups(field: ReferenceField, principal: Principal) -> list[_Group]:
    """The infrastructure a column takes, one group per kind, read off the topology."""

    known = targets()
    kinds = [kind for kind in (*ESTATE_KINDS, *_certificate_kinds()) if kind not in known and field.accepts(kind)]
    estate = _estate(principal) if kinds else {}
    return [
        (
            _plural(kind).capitalize(),
            _by_name((str(Reference(kind, identity)), name) for identity, name in estate.get(kind, {}).items()),
        )
        for kind in kinds
    ]


def _record_groups(field: ReferenceField, principal: Principal) -> list[_Group]:
    """The records a column takes that this principal may name, one group per
    model, read in one statement."""

    picked = [
        target
        for target in targets().values()
        if target.declared.pickable and field.accepts(target.kind) and _may_name(target, principal)
    ]
    rows = _rows([(target.model, target.columns, Q()) for target in picked])
    return [
        (
            target.plural.capitalize(),
            _by_name(
                (str(Reference(target.kind, str(getattr(row, target.declared.key)))), target.name_of(row))
                for row in found
            ),
        )
        for target, found in zip(picked, rows, strict=True)
    ]


def _plural(kind: str) -> str:
    """An infrastructure kind's plural noun: its page's, or its registry noun's."""

    return NODE_KINDS[kind].plural if kind in NODE_KINDS else f"{_noun(kind).lower()}s"


def _by_name(options: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Options in the order a reader looks for them: by name, whatever its case."""

    return sorted(options, key=lambda option: option[1].casefold())


# ----- What names a thing --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mention:
    """One row that names a thing: its link, and a few words beside it."""

    link: EntityLink
    note: str = ""


@dataclass(frozen=True, slots=True)
class Mentions:
    """A group of things related to a thing, under one heading: the rows that
    name it through one kind of reference, or what its own record links."""

    heading: str
    items: tuple[Mention, ...]


def referenced_by(kind: str, identity: str, *, principal: Principal) -> tuple[Mentions, ...]:
    """Every row that names this thing, grouped under each reference's heading.

    One statement for the page, whatever number of models hold a reference.
    """

    value = str(Reference(kind, str(identity)))
    known = {target.model: target for target in targets().values()}
    fields = [
        field
        for field in reference_fields()
        if field.accepts(kind) and (field.model not in known or _may_name(known[field.model], principal))
    ]
    rows = _rows([(field.model, _unique(("pk", *field.shows)), Q(**{field.attname: value})) for field in fields])
    groups: dict[str, list[Mention]] = defaultdict(list)
    for field, found in zip(fields, rows, strict=True):
        for row in found:
            groups[field.heading].append(
                Mention(
                    entity_links.EntityLink(
                        # Named as the model names it everywhere else.
                        label=known[field.model].name_of(row) if field.model in known else str(row),
                        url=row.get_absolute_url(),
                        kind=field.model._meta.label_lower,
                        kind_label=str(field.model._meta.verbose_name).capitalize(),
                    ),
                    str(getattr(row, field.note) or "") if field.note else "",
                )
            )
    return tuple(Mentions(heading, tuple(items)) for heading, items in groups.items())


def referenced_by_row(row: models.Model, *, principal: Principal) -> tuple[Mentions, ...]:
    """``referenced_by`` for a row of a model that declares ``referable``."""

    for target in targets().values():
        if isinstance(row, target.model):
            return referenced_by(target.kind, str(getattr(row, target.declared.key)), principal=principal)
    raise LookupError(f"{type(row).__name__} does not declare that it can be referred to.")


# ----- A reference that names nothing ---------------------------------------------


def dangling(
    *models_: type[models.Model],
    principal: Principal | None = None,
    source: Callable[[models.Model], str] = str,
) -> tuple[Insight, ...]:
    """One item for the queue per row whose reference names nothing.

    Three statements whatever the number of rows: every distinct reference
    the models hold, the records those name, then the rows holding one that
    names nothing. No models means
    every installed model, and no principal means HQ reading its own records.
    """

    principal = principal or _reader()
    fields = [field for field in reference_fields() if not models_ or field.model in models_]
    if not fields:
        return ()
    gone = [
        (field, values)
        for field, values in zip(fields, _naming_nothing(_held(fields), principal), strict=True)
        if values
    ]
    parts = [
        (
            field.model,
            _unique(("pk", field.attname, field.name_attname, *field.shows)),
            Q(**{f"{field.attname}__in": values}),
        )
        for field, values in gone
    ]
    return tuple(
        _dangling_item(field, row, source)
        for (field, _values), rows in zip(gone, _rows(parts), strict=True)
        for row in rows
    )


def _naming_nothing(held: list[set[str]], principal: Principal) -> list[set[str]]:
    """Of each column's references, the ones that name nothing, in one read of
    what they could name."""

    parsed = {value: Reference.parse(value) for values in held for value in values}
    named = _found({reference for reference in parsed.values() if reference}, principal=principal)
    return [{value for value in values if _names_nothing(parsed[value], named, principal)} for values in held]


def _names_nothing(reference: Reference | None, named: Mapping[Reference, EntityLink], principal: Principal) -> bool:
    """Whether a reference names nothing. One of a kind this principal may not
    be told about is not reported to it, named or not."""

    if reference is None:
        return True
    target = targets().get(reference.kind)
    hidden = target is not None and not _may_name(target, principal)
    return reference not in named and not hidden


def _dangling_item(field: ReferenceField, row: models.Model, source: Callable[[models.Model], str]) -> Insight:
    """The queue's item for one row whose reference names nothing."""

    name = getattr(row, field.name_attname) or getattr(row, field.attname)
    return Insight(
        status="attention",
        eyebrow="Links",
        title=f"{source(row)} links to {name}, which HQ does not have",
        value="",
        body="Choose something else for it, or clear the link.",
        action="Open record",
        url=row.get_absolute_url(),
        key=f"reference:{field.model._meta.label_lower}:{row.pk}:{field.attname}",
        family="Links to nothing",
        # A row's own link, checked against every record and reading at once.
        observed_at=None,
    )


def _held(fields: Sequence[ReferenceField]) -> list[set[str]]:
    """Every distinct reference each column holds, in one statement."""

    queries = [
        field.model._default_manager.exclude(**{field.attname: ""})
        .order_by()
        .values_list(Value(index, output_field=models.IntegerField()), field.attname)
        .distinct()
        for index, field in enumerate(fields)
    ]
    combined = queries[0].union(*queries[1:]).order_by() if len(queries) > 1 else queries[0]
    held: list[set[str]] = [set() for _ in fields]
    for index, value in combined:
        held[index].add(value)
    return held
