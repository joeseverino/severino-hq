"""Dependency-free contracts a domain uses to emit its own connection.

Here rather than in ``connections`` because a domain gateway declares its
connection, and ``connections`` reads back into the domains to compose the
registry: sharing the reading module would close an import cycle. These are
plain records, so a declaration costs the declaring module nothing.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, get_args

from .contracts import Effect
from .describe import Describe
from .display_words import as_members
from .security import Requires
from .tone import Tone

# Named for annotations alone: a class's annotations can be read as it is made
# (a dataclass's are), which would load a lazy import at once.
if TYPE_CHECKING:
    from .security import Capability

# How authority for an ability is proven, declared once per ability.
#
#   scoped  the provider grants narrow permissions; the ability lists the ones
#           it needs in ``required_scopes`` and HQ checks them against what the
#           instance reports
#   coarse  the credential is the whole account and the provider offers nothing
#           narrower: a login key, an admin token; evidence exists, least
#           privilege does not
#   none    keyless; there is no credential, so there is no grant to prove
#
# Left blank, the ability has declared nothing, and HQ says so rather than
# calling it authorized because nobody asked.
GrantModel = Literal["scoped", "coarse", "none"]
GRANT_MODELS: tuple[str, ...] = get_args(GrantModel)

# How an instance's credential was observed, from the same vocabulary, plus
# ``rejected`` for one the provider refused.
CREDENTIAL_MODELS: tuple[str, ...] = (*GRANT_MODELS, "rejected")
# A credential HQ holds but cannot classify: a controller's report of a
# provider HQ no longer models. Said rather than left blank, so no reader has
# to guess what an empty value meant.
UNKNOWN_CREDENTIAL = "unknown"
# Every value an instance may say.
INSTANCE_CREDENTIAL_MODELS = (*CREDENTIAL_MODELS, UNKNOWN_CREDENTIAL)


@dataclass(frozen=True, slots=True)
class ConnectionAbility:
    """One thing a connection permits HQ to do, without credential material."""

    name: str
    label: str
    summary: str
    # Given as its value's string too, as an extension may write it.
    effect: Effect = Effect.READ
    required_scopes: tuple[str, ...] = ()
    # Blank, or how authority is proven. Required scopes are ``scoped``; the
    # other two are how an ability says, truthfully, that no scope check can
    # exist.
    grant: GrantModel | Literal[""] = ""
    capability: str = ""
    # Resource kinds this ability governs. The relation is explicit because an
    # ability name describes what a connection can do; it is not inherently a
    # ManagedResource kind, and separate connection families may reuse it.
    governs_kinds: tuple[str, ...] = ()
    # The canonical resource catalog these governed kinds belong to. Commands
    # against that resource can then be discovered from this ability without a
    # provider-specific command list.
    subject_resource: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect", Effect(self.effect))
        if self.grant not in ("", *GRANT_MODELS):
            raise ValueError(f"Ability {self.name!r} has grant {self.grant!r}, none of {', '.join(GRANT_MODELS)}.")
        if self.grant and (self.grant == "scoped") != bool(self.required_scopes):
            raise ValueError(f"Ability {self.name!r} lists required scopes exactly when its grant is scoped.")


@dataclass(frozen=True, slots=True)
class ConnectionLink:
    """A safe relationship from a connection to something HQ can name."""

    label: Annotated[str, Describe("What it is, by name.")]
    url: Annotated[str, Describe("Its page; empty for none.")] = ""
    # Explicit identity for a ManagedResource dependency. A rendered label is
    # presentation and must never become a join key by coincidence.
    resource_key: Annotated[str, Describe("The key of HQ's record it is; empty when it is none.")] = ""


@dataclass(frozen=True, slots=True)
class ConnectionFact:
    """A small provider-owned fact that is useful in a generic connection row."""

    label: Annotated[str, Describe("What the fact is about, in a few words.")]
    value: Annotated[str, Describe("The fact, as the connection reported it.")]


@dataclass(frozen=True, slots=True)
class ConnectionInstance:
    """One configured connection as its owning domain last observed it."""

    id: str
    label: str
    kind: str
    # How its owner last found it. One that says nothing is read from what came
    # through it (``credential_sight.read_through``), its state in its lifecycle's word.
    status: Tone = Tone.NEUTRAL
    status_label: str = ""
    detail: str = ""
    endpoint: str = ""
    observed_at: datetime | None = None
    granted_scopes: tuple[str, ...] = ()
    scopes_known: bool = False
    ability_names: tuple[str, ...] = ()
    targets: tuple[ConnectionLink, ...] = ()
    dependencies: tuple[ConnectionLink, ...] = ()
    facts: tuple[ConnectionFact, ...] = ()
    # The observer that supplied this reading. Optional because an extension
    # may read an account directly rather than through an infrastructure
    # controller; when present it gives topology a real edge instead of asking
    # a rendered label to carry identity.
    controller_id: str = ""
    # The ref audit events name this connection by, or blank.
    connection_ref: str = ""
    # One of INSTANCE_CREDENTIAL_MODELS, and required: what kind of credential
    # this is, as distinct from which grants it carries (a scoped provider's
    # token whose grants were never enumerated is still a scoped credential).
    # Every reader trusts it, so a connection that does not say is refused as
    # it is made, rather than drawn as one with a secret nobody named.
    credential_model: str = field(kw_only=True)

    @property
    def keyless(self) -> bool:
        """Whether it reaches its service with no credential at all."""

        return self.credential_model == "none"

    def __post_init__(self) -> None:
        as_members(self)
        if self.credential_model not in INSTANCE_CREDENTIAL_MODELS:
            raise ValueError(
                f"Connection {self.id!r} must say its credential_model, one of "
                f"{', '.join(INSTANCE_CREDENTIAL_MODELS)}; it says {self.credential_model!r}."
            )


@dataclass(frozen=True, slots=True)
class ConnectionSpec(Requires):
    """One declaration of a connection family and its cached instance provider."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    instance_provider: Callable[[], tuple[ConnectionInstance, ...]]
    abilities: tuple[ConnectionAbility, ...] = ()
    web_route: str = "control_plane:connections"
    management_route: str = ""
    setup_route: str = ""
    documentation_url: str = ""
    secret_store: str = ""
    # What this family's emptiness means, in its own words. The default suits a
    # family that emits from configuration; one fed by controller reports
    # overrides it to say so.
    empty_message: str = "Not set up here yet."
