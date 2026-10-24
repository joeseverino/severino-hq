"""How to connect each provider, derived from what HQ already declares.

A connection is a 1Password item the controller is given (docs/Secrets.md,
"Connections"). Everything a person needs to make one is declared somewhere
already: what to create at the provider (its ``ConnectionKind``), the
permissions its readings need (``observer_permissions``), the fields its
credential arrives in (its ``ConnectionShape``) and a script that mints it
where one exists (``MINTERS``). This puts them together, once, for the
Connections page and the reference doc both, so neither can say something the
code does not.
"""

from dataclasses import dataclass

lazy from django.conf import settings

from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
from hq.domains.control_plane.credential_reads import MINTERS, observer_permissions
from hq.domains.control_plane.provider_spec import ConnectionShape, Setting

lazy from .credential_sight import credential_sight
lazy from .entity_links import web_url
lazy from .page_reads import need

# 1Password's own names for the built-in fields a setting can come from.
_BUILT_IN = {"credential": "credential", "password": "password", "username": "username"}


@dataclass(frozen=True, slots=True)
class ItemField:
    """One field on the 1Password item: its name, what to put in it, and why."""

    name: str
    value: str
    note: str = ""
    secret: bool = False


@dataclass(frozen=True, slots=True)
class ProviderSetup:
    provider: str
    label: str
    # "scoped": the provider grants narrow permissions; "coarse": the
    # credential is the whole account.
    credential: str
    create: str
    create_url: str
    permissions: tuple[str, ...]
    mint: str
    item_type: str
    fields: tuple[ItemField, ...]
    sees: tuple[str, ...]
    manages: tuple[str, ...]
    # What connecting it gives, in the owner's words (``ConnectionKind.gives``).
    gives: str = ""

    @property
    def item(self) -> str:
        """The item to store it as, said as a phrase: "a Login item"."""

        if self.item_type == "any item type":
            return "an item of any type"
        return f"{'an' if self.item_type[0] in 'AEIOU' else 'a'} {self.item_type} item"


def docs_url(page: str, anchor: str = "") -> str:
    """One of HQ's own docs, in the source it names; blank when it names none."""

    source = web_url(settings.SEVERINO_HQ_SOURCE)
    return f"{source}/blob/main/docs/{page}" + (f"#{anchor}" if anchor else "") if source else ""


def guide_url() -> str:
    """Where connections are written down."""

    return docs_url("Secrets.md", "connections")


def setups(exclude: frozenset[str] = frozenset()) -> tuple[ProviderSetup, ...]:
    """Every provider HQ can connect to, but those in ``exclude``, in the order
    to connect them: what the dashboard reads first, then what the most pages
    read (``page_reads.need``), then by name."""

    sight = {found.provider: found for found in credential_sight(inventory={})}
    needed = need()

    def order(setup: ProviderSetup) -> tuple[bool, int, str]:
        dashboard, pages = needed.get(setup.provider, (False, 0))
        return (not dashboard, -pages, setup.label)

    return tuple(
        sorted(
            (_setup(provider, sight.get(provider)) for provider in CONNECTION_KINDS if provider not in exclude),
            key=order,
        )
    )


def _setup(provider: str, sight) -> ProviderSetup:
    kind = CONNECTION_KINDS[provider]
    minter = MINTERS.get(provider)
    return ProviderSetup(
        provider=provider,
        label=kind.label,
        credential=kind.credential,
        create=kind.create,
        create_url=kind.create_url,
        permissions=tuple(sorted({*observer_permissions(provider), *kind.writes}))
        if kind.credential == "scoped"
        else (),
        mint=" ".join(minter.invocation) if minter else "",
        item_type=_item_type(kind.shape),
        fields=_fields(provider, kind.shape),
        sees=tuple(dict.fromkeys(seen.label for seen in sight.sights)) if sight else (),
        manages=sight.manages if sight else (),
        gives=kind.gives,
    )


def _item_type(shape: ConnectionShape) -> str:
    """The 1Password item type whose built-in fields the shape reads."""

    built_in = {setting.item_field for setting in shape.settings if setting.item_field}
    if "credential" in built_in:
        return "API Credential"
    if built_in & {"username", "password"}:
        return "Login"
    return "any item type"


def _fields(provider: str, shape: ConnectionShape) -> tuple[ItemField, ...]:
    """The item's fields: the three that make it a connection, then the credential's."""

    return (
        ItemField("connection_ref", provider.replace("_", "-"), "Any name; how HQ refers to this connection."),
        ItemField("projection", shape.name, "The shape the controller reads the item as."),
        ItemField("env_prefix", provider.upper(), "Says which service it is for."),
        *(_field(setting) for setting in shape.settings),
        ItemField("manages", "1", "Optional. Lets HQ make changes through it, not only read."),
    )


def _field(setting: Setting) -> ItemField:
    optional = " Optional." if setting.optional else ""
    if setting.item_field:
        name = _BUILT_IN.get(setting.item_field, setting.item_field)
        return ItemField(name, "", f"The item's own {name} field.{optional}", setting.secret)
    if setting.url is not None:
        return ItemField("website", "", f"The item's website: the service's address.{optional}", setting.secret)
    return ItemField(setting.label, "", f"A field labeled {setting.label}.{optional}", setting.secret)


REFERENCE_HEADER = """# Connections

What to create for each service HQ connects to, in the order to connect them,
and the 1Password item the controller reads it from. Written by `manage.py
connections_reference` from what HQ expects of each service; the Connections
page shows the same, for each one not yet connected. A test fails when this
file falls behind.

How a connection item works in general is in [Secrets](Secrets.md#connections).
"""


def reference_markdown() -> str:
    """docs/Connections.md: every provider's setup, as the page shows it."""

    return REFERENCE_HEADER + "".join(_reference_section(setup) for setup in setups())


def _reference_section(setup: ProviderSetup) -> str:
    lines = ["", f"## {setup.label}", "", setup.gives, "", f"{setup.create} [Where to make it]({setup.create_url})", ""]
    if setup.permissions:
        lines += ["Permissions, and no others:", "", *(f"- `{permission}`" for permission in setup.permissions), ""]
    elif setup.credential == "coarse":
        lines += [f"This is the whole account: {setup.label} offers nothing narrower, so make it for HQ alone.", ""]
    if setup.mint:
        lines += [f"Or have `{setup.mint}` issue one with exactly these.", ""]
    lines += [f"Store it as {setup.item}:", "", "| Field | Value | What it is |", "| --- | --- | --- |"]
    for field in setup.fields:
        value = f"`{field.value}`" if field.value else ("the secret" if field.secret else "yours")
        lines.append(f"| `{field.name}` | {value} | {field.note} |")
    return "\n".join(lines) + "\n"
