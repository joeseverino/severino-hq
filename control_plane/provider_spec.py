"""What a provider declaration is made of, and nothing that declares one.

Every provider module builds its declaration from these, and the registry in
``control_plane.providers`` derives everything else from what they emit. Kept
apart from both so a provider can import its vocabulary without importing the
registry that collects it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from django.urls import NoReverseMatch, reverse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from application.ui import counted

from .names import in_zone


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _settle_line_endings(cls, value: Any) -> Any:
        """Store one newline, whatever the browser sent.

        HTML submits a textarea as CRLF and every provider returns LF, so a
        multi-line field saved through a form never again equals the identical
        document read back. Normalised on the way in, so the declaration and
        the reading are comparable at all, and asked of every string field
        rather than the two that have textareas today. Runs ``after`` because
        a ``before`` validator is not allowed on a discriminated union's
        discriminator, and this one matches every field.
        """

        if isinstance(value, str) and "\r" in value:
            return value.replace("\r\n", "\n").replace("\r", "\n")
        return value


class ControllerVerification(ProviderModel):
    timeout_seconds: int = Field(ge=1, le=3600)
    interval_seconds: int = Field(ge=1, le=300)

    @model_validator(mode="after")
    def interval_fits_timeout(self):
        if self.interval_seconds > self.timeout_seconds:
            raise ValueError("verification interval must not exceed its timeout")
        return self


class ControllerActionPolicy(ProviderModel):
    mode: Literal["apply", "locked"]
    automatic: bool = False
    reason: str = ""
    verification: ControllerVerification | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "locked" and self.automatic:
            raise ValueError("locked controller actions cannot be automatic")
        if self.mode == "locked" and not self.reason:
            raise ValueError("locked controller actions require a reason")
        return self


class ControllerProviderCapability(ProviderModel):
    actions: dict[str, ControllerActionPolicy] = Field(min_length=1)


class ControllerCapabilityRegistry(ProviderModel):
    schema_version: Literal[1]
    capabilities: dict[str, ControllerProviderCapability]


def applies(*, automatic: bool = False, verification=None) -> ControllerActionPolicy:
    """The controller may run this action."""

    return ControllerActionPolicy(
        mode="apply", automatic=automatic, verification=verification
    )


def locked(reason: str) -> ControllerActionPolicy:
    """The controller may not run this action, and this is why.

    The reason is shown to whoever pressed the button, so it says what is true
    rather than that a policy said no.
    """

    return ControllerActionPolicy(mode="locked", reason=reason)

def origin_is_authoritative(provider: "ProviderSpec") -> bool:
    """Whether this provider's origin says where a request is *finally* served.

    Two kinds of provider answer "and then what serves it", and they mean
    different things by it. One that also *answers* for the name states where
    the name points, which for a proxied name is the proxy. One that only
    routes states where the request ends up. Both are origins; only the second
    is the answer to "what serves this", so the first has to yield wherever both
    are present.

    Stated once, here, because two surfaces rank origins (the service catalogue
    and the machine board) and must agree.
    """

    return provider.answers is None


# The facets a service is assembled from, in the order a request meets them: a
# name has to resolve, something has to answer for it, and the TLS it answers
# with has to cover it.
#
# Declared here beside the providers rather than wherever services are composed,
# because a provider names the facet it supplies. A provider added later joins
# the service view by declaring one, and nothing else holds a list of what can
# participate.
# The order columns are rendered in, and the order a name is wired in: something
# has to run before ingress can reach it, and ingress before a certificate
# secures it.
SERVICE_FACETS: tuple[tuple[str, str], ...] = (
    ("runtime", "Runtime"),
    ("dns", "DNS"),
    ("proxy", "Ingress"),
    ("certificate", "Certificate"),
)
SERVICE_FACET_IDS = frozenset(facet for facet, _ in SERVICE_FACETS)

@dataclass(frozen=True)
class NameContext:
    """What HQ already knows about a hostname, offered to the next question.

    Every field here was worked out somewhere else on the way in: which zones a
    credential may edit, where something already answers this name, which
    certificate already covers it. Passed rather than re-derived, because a form
    that cannot see them asks for them again, and a page offering to issue a
    Let's Encrypt certificate for a name in no public zone is not asking, it is
    proposing a failure.

    Declared here beside the providers that read it and built in the application
    layer, which is the half allowed to touch the database. Every field
    defaults, so a caller that knows nothing yet is a legal caller.
    """

    hostname: str = ""
    # Zones a connected credential can actually edit, as the controller last
    # reported them. Empty means nothing has swept, not that nothing is
    # reachable, so an empty tuple must never be read as a prohibition.
    public_zones: tuple[str, ...] = ()
    swept: bool = False
    # Where this name is already served, as "host:port", declared or observed.
    # The host half is whatever the provider calls the machine, which for a
    # container stack is the machine's name rather than an address.
    origin: str = ""
    # The same place, as something on the network can actually reach it. A
    # proxy seeded with a machine name is seeded with something nginx cannot
    # resolve, which is a worse answer than an empty box: it looks considered.
    origin_address: str = ""
    # Resource keys of certificates that already cover this name.
    certificates: tuple[str, ...] = ()

    @property
    def public_zone(self) -> str:
        """The reported zone this name falls in, if one does.

        Suffix-matched on label boundaries: "notexample.com" is not in
        "example.com", and a check on plain string endings says it is.
        """

        return next((zone for zone in self.public_zones if in_zone(self.hostname, zone)), "")


@dataclass(frozen=True)
class ProviderSpec:
    kind: str
    # What this is called in a sentence, and what it does in one line. Both are
    # read by people: "adguard.rewrite" is the identifier, not the name, and a
    # page that offers it as a choice has to say what choosing it means.
    summary: str
    spec_type: type
    resolved_type: type | None = None
    resolver: (
        Callable[[dict[str, Any], "ProviderResolutionContext"], dict[str, Any]] | None
    ) = None
    destructive: bool = False
    public_effect: bool = False
    # Declared after the positional fields, and always passed by keyword: the
    # existing entries pass resolved_type and resolver positionally, so a new
    # field inserted above them silently rebinds both.
    label: str = ""

    # ----- Service participation ---------------------------------------------
    #
    # A service is a hostname and everything that has to be true for it to
    # answer. A provider joins that view by naming the facet it supplies and
    # saying how to read the hostnames out of a *resolved* spec. A provider that
    # names neither simply does not appear there, so nothing needs an exclusion
    # list to keep it out.
    facet: str = ""
    hostnames: Callable[[dict[str, Any]], tuple[str, ...]] | None = None
    # Whether this provider *covers* hostnames rather than declaring them.
    # Declaring brings a service into existence: something has to name it
    # before it is a thing at all. Covering answers for a set that may include
    # wildcards, so it attaches to services declared elsewhere and never invents
    # one: treated as a declaration, a wildcard certificate would conjure a
    # service literally called "*.example.com".
    covers: bool = False
    # Where a request for these hostnames is finally served, as "host:port".
    # Only an ingress provider has one.
    origin: Callable[[dict[str, Any]], str] | None = None
    # The inverse of ``hostnames``: the spec fields that follow from being told
    # a hostname. Onboarding a service asks for the name once and seeds every
    # provider that declares a facet for it, so the operator types it once
    # rather than once per resource, and a provider added later joins that
    # flow by saying which of its fields the name fills in.
    seed: Callable[["NameContext"], dict[str, Any]] | None = None
    # Some resources are not complete without material the operator has to
    # supply: an uploaded certificate is only a name and a list of targets
    # until the certificate itself arrives. Declared as a form and a handler so
    # the same page collects both: asked for separately, creating one produced
    # an empty declaration and a second page to go and find.
    material_form: str = ""
    material_handler: str = ""
    # Fields that are routine tuning rather than part of the question being
    # asked. Split on required-ness instead, a spec whose validity comes from a
    # cross-field rule has no required fields at all, and its form rendered
    # empty. Required-ness describes the model; this describes the conversation,
    # and only the provider knows which of its own knobs are which.
    advanced_fields: tuple[str, ...] = ()
    # What changing a field actually causes, as ``((field, sentence), ...)``.
    # Saving a new name onto a certificate is not "saving": HQ notices the
    # deployed certificate no longer covers what is declared and re-issues it
    # within the minute. The page that takes the edit is the only place that
    # can say so beforehand, and a provider is the only thing that knows.
    change_effects: tuple[tuple[str, str], ...] = ()
    # Fields that are optional to the model but unanswerable-by-default when
    # the record does not exist yet. An NPM proxy keeps whatever certificate it
    # already has when this is blank, which is a sensible default for an edit
    # and a guaranteed failure on create: the reconciler refuses to create an
    # HTTPS host with no certificate to bind, a minute later, in a job result.
    required_on_create: tuple[str, ...] = ()
    # Fields the provider structurally cannot report back, so a sweep must not
    # be read as disagreeing about them.
    #
    # ``from_record`` serves two callers with opposite readings of a blank. To
    # adoption it means "say nothing and keep what is there"; to the drift
    # comparison it means "the live record says empty". An NPM proxy host is
    # the case: NPM holds a numeric certificate id, not an HQ resource key.
    unobservable_fields: tuple[str, ...] = ()
    # Why nothing sweeps this kind, or "" when something does.
    #
    # The collector registry is a dict in the controller and this is the list
    # of kinds; nothing joined them, so a kind could be declared here and swept
    # by nothing at all, with no symptom but a staleness claim no sweep clears.
    #
    # A sentence rather than a boolean: "nothing can reach it" and "no
    # collector has been written yet" are different states, and the second is
    # work rather than a fact about the world.
    unobserved_reason: str = ""
    # Why removing this declaration is not offered, or "" when it is.
    #
    # Removal queues a controller delete unless the provider is
    # `declaration_only`, so a kind whose controller implements no delete has
    # declarations that cannot be removed. Stated here rather than discovered
    # at the point somebody tries.
    removal_gap: str = ""
    # The page a resource of this kind lives on, when it has one of its own.
    home: Callable[[Any], str] | None = None
    # The provider console page for a swept record, built only from ids the
    # record stores. Blank when the record lacks them.
    console: Callable[[dict[str, Any]], str] | None = None
    # Whether a record puts its provider in front of the names it carries: a
    # proxied Cloudflare record. A reading whose ``fronted_by`` names this kind
    # applies to a name only where this says so.
    fronts: Callable[[dict[str, Any]], bool] | None = None
    # From an observed record: the source policy it applies to its names, as a
    # ``provider_adapters.contracts.IngressPolicy``.
    ingress_policy: Callable[[dict[str, Any]], Any] | None = None
    # From an observed record: the certificate it serves its names with, as a
    # ``provider_adapters.contracts.ServedCertificate``, or None.
    served_certificate: Callable[[dict[str, Any]], Any] | None = None
    # Headers a proxy of this kind adds to corroborate the forwarded client
    # and scheme, in that order.
    forwarding_headers: tuple[str, str] | tuple[()] = ()
    # The certificate declaration an ingress serves its names with, by
    # resource key, or "" when it names none. A covering certificate applies
    # to a name only where the ingress that serves it names that certificate.
    certificate: Callable[[dict[str, Any]], str] | None = None
    # One record shaped exactly as this provider's sweep reports them, for the
    # contract tests to rebuild a spec from. It lives beside the provider
    # because a list the tests keep is a list that goes stale.
    sample_record: dict[str, Any] | None = None
    # Whether a sweep may take on a record it found without asking. None: every
    # record, the default for any kind whose records only exist because a
    # connection HQ was given created them. A kind that can also see things
    # nobody declared anywhere says which of its records are safe to adopt.
    adopts: Callable[[dict[str, Any]], bool] | None = None
    # What sorts of connection stand behind this, matching what the controller
    # calls them. This is the join between a declaration and the credentials
    # that would carry it out: it tells a form which connections to offer, and
    # the connections page what each one is for.
    #
    # Plural because one resource can need two unrelated credentials: a managed
    # certificate is issued through a DNS token and installed over SSH.
    connection_providers: tuple[str, ...] = ()
    # Why this provider cannot supply a given name, or "" when it can: a
    # `.home.arpa` name cannot get a Let's Encrypt certificate, whose DNS-01
    # challenge needs a zone a credential holds. A sentence rather than a
    # boolean, because the page says why.
    applies: Callable[["NameContext"], str] | None = None
    # ``module:attribute`` returning ``{field: ((value, label), ...)}`` for the
    # fields whose valid answers are a matter of live data rather than of type.
    # A topology reference is the case that forced it: rendered from the
    # annotation alone it is a blank text box that only works if you already
    # know the exact slug to type into it, which is not a form, it is a quiz.
    #
    # Late-bound as a string, the same way domains reference their providers, so
    # this module keeps declaring and stays free of database access.
    choices: str = ""
    # Turns a record the provider already holds into the spec that would
    # reproduce it. This is what makes adoption safe: the declaration starts out
    # equal to the world, so the first reconciliation after adopting changes
    # nothing. Built from the same field set the reconciler sends, so a setting
    # HQ can express is a setting adoption captures.
    from_record: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # What this resource actually does, as (label, desired, observed) rows.
    # A service page showed "Declared" in the largest type on the card while the
    # row beneath it held `answer: 10.0.0.10`: the least useful fact rendered
    # loudest, and the useful one not rendered at all. Desired and observed sit
    # side by side because the interesting case is when they differ, and either
    # may be blank: a certificate has no authored expiry, only a found one.
    readout: (
        Callable[[dict[str, Any], dict[str, Any]], tuple[tuple[str, str, str], ...]]
        | None
    ) = None
    # ----- Identity ----------------------------------------------------------
    #
    # How to tell that a live record and a declaration are the same thing.
    #
    # This defaults to ``hostnames`` and for two providers that is exactly
    # right: an AdGuard rewrite and an NPM proxy host are each the only record
    # their name can have, so "same name" and "same record" mean the same thing.
    #
    # They stop meaning the same thing the moment a provider holds several
    # records for one name. A zone apex routinely carries three TXT records,
    # four CAA records, two MX records and a CNAME: nine distinct records, one
    # hostname. Identified by hostname they collapse into one, and adoption
    # picks whichever the provider happened to list first. Worse, the types that
    # carry policy rather than address deliberately declare no hostname at all,
    # so they would report as having no identity and be permanently invisible to
    # the one screen built to find unmanaged things.
    identity: Callable[[dict[str, Any]], tuple[str, ...]] | None = None
    # A readable key to suggest when adopting. Defaults to the hostname and the
    # facet, which is meaningless for a record that has no hostname: every TXT
    # record in a zone would be offered the same empty name.
    key_hint: Callable[[dict[str, Any]], str] | None = None
    # The surface that offers creating one, when it is not the registry's own
    # "what do you want to add?" page. A public DNS record is only meaningful
    # inside a zone: offered from the generic page it has to open by asking
    # which domain, which is the one question the page it belongs on has
    # already answered. Declared here rather than excluded there, so the picker
    # never grows a hand-maintained list of the kinds it is meant to leave out.
    created_from: str = ""
    # What stops working if this particular resource is removed, in a sentence.
    # Read by the confirmation page, which otherwise asks "are you sure" about a
    # row of fields, and the honest answer to that depends entirely on which
    # row it is. Deleting one of four CAA records is housekeeping; deleting the
    # last MX record stops the domain receiving mail.
    removal_note: Callable[[dict[str, Any]], str] | None = None
    # Whether this declaration describes something HQ made at a provider, or
    # only records a responsibility HQ was given.
    #
    # Removal assumes the first, correctly for almost everything: a rewrite, a
    # proxy host and a DNS record all exist somewhere else, so forgetting the
    # row alone would abandon them. A domain is the exception. HQ did not create
    # the zone and deleting it would be absurd; being responsible for it is the
    # entire content of the declaration, so removing it is ceasing to be
    # responsible.
    declaration_only: bool = False
    # Whether other resources resolve against this one. Saving it changes what
    # they mean without touching what they say, so their desired state has to be
    # recomputed, otherwise a certificate reports itself in sync against a
    # target that moved underneath it.
    resolution_input: bool = False
    # Whether changing this kind needs a person to agree, and not merely a
    # caller that holds the capability.
    #
    # Declared per kind rather than per capability effect. Effect says how
    # forceful an act is; it cannot say how much is standing behind the thing
    # being acted on. Reconciling a rewrite and rewriting the estate's access
    # policy are the same effect and not remotely the same event, and a rule
    # written on the effect would either wave the second one through or put a
    # decision in front of a person for every container restart: at which
    # point the decision stops being read.
    #
    # A kind flagged here holds changes asked for over any interface that is
    # not a person at a browser: a credential on its own is not enough.
    requires_approval: bool = False
    # The addresses a record makes a name resolve to, where it resolves to an
    # address at all. Declared by the provider because only it knows which of
    # its fields is the answer, and read by anything asking who can reach a
    # name, which is a property of the address rather than of the record.
    answers: Callable[[dict[str, Any]], tuple[str, ...]] | None = None
    # What this declaration holds, as ``(kind, their_field, my_field)``.
    #
    # A domain holds the records published in it, and ceasing to be responsible
    # for the domain has to release them: left behind, HQ would keep
    # reconciling records in a zone the operator had just said was not its
    # business. Which resources those are is provider knowledge, so the
    # generic forget path names no kind.
    contains: tuple[str, str, str] | None = None
    # ``module:function`` returning ``{field: {value: note}}``, which of the
    # values already in a field HQ can see without being told. A dotted path for
    # the same reason ``choices`` is one: the answer needs the database, and
    # this module must not.
    notes: str = ""
    # What the controller may do to this kind, and which of those it may do
    # unprompted. Declared here, beside the provider it is about.
    #
    # This lived in `config/controller-capabilities.json`, a hand-kept file that
    # had to name every provider exactly once or HQ refused to start. So a new
    # provider was two edits in two languages, and the file could only ever
    # repeat what the registry already knew. HQ is the source of truth for what
    # HQ can do; a second copy of that is a thing to keep in sync, not a
    # contract.
    actions: Mapping[str, ControllerActionPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.facet and self.facet not in SERVICE_FACET_IDS:
            raise ValueError(
                f"Provider {self.kind!r} declares unknown service facet "
                f"{self.facet!r}; expected one of {sorted(SERVICE_FACET_IDS)}."
            )
        if (self.covers or self.hostnames or self.origin) and not self.facet:
            raise ValueError(
                f"Provider {self.kind!r} describes hostnames but names no "
                "service facet, so nothing would ever read them."
            )

    def schema(self) -> dict[str, Any]:
        return TypeAdapter(self.spec_type).json_schema()

    def validate(self, payload: dict[str, Any]):
        return TypeAdapter(self.spec_type).validate_python(payload)


@dataclass(frozen=True)
class ProviderResolutionContext:
    # Every place a certificate can be installed, as HQ holds them. Passed in
    # rather than queried here so this module stays free of the database and a
    # projection resolving many resources pays for one read.
    delivery_targets: tuple[dict[str, Any], ...] = ()
    # ``(key, kinds) -> status``. Kinds rather than one kind because a proxy
    # host can be bound to a certificate HQ issued or one it was given, and it
    # names the resource without saying which it is.
    resource_status: Callable[[str, tuple[str, ...]], dict[str, Any] | None] | None = (
        None
    )
    # The key of the resource being resolved, where resolution depends on which
    # resource is asking: a target's name belongs to one certificate, and the
    # rest are named after themselves.
    resource_key: str = ""
    # Every Caddy route HQ declares. A route reconciles by writing the file its
    # edge imports, and that file is all of them at once, so one route's
    # contract has to carry its siblings, the way a certificate's carries every
    # place it installs.
    #
    # Called rather than passed, because resolution runs for every declaration
    # on a page and only a route asks this.
    caddy_routes: Callable[[], tuple[dict[str, Any], ...]] | None = None
    # ``connection_ref -> hostnames observed landing on that connection's
    # machine``. Passed in for the same reason the targets are: this module
    # states what a certificate installs and must not be the thing that queries
    # a sweep to find out.
    #
    # Every site that resolves a spec has to supply it, including the one that
    # fingerprints desired state, or the generation advances every time it is
    # computed.
    names_at: Callable[[str], tuple[str, ...]] | None = None


def expiry_phrase(stamp: str) -> str:
    """An expiry a person can act on: the date, and how long that leaves.

    The raw ISO timestamp is what the provider reports and the wrong thing to
    print. "2026-10-23T22:00:38+00:00" has to be read and subtracted from today
    before it means anything, and the number it resolves to (how many days are
    left) is the entire reason anyone looks at it.
    """

    try:
        expires = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return stamp or ""
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    from application.expiry import days_until

    days = days_until(expires)
    if days < 0:
        return f"{expires:%-d %b %Y} · expired"
    return f"{expires:%-d %b %Y} · {counted(days, 'day')}"


def named_page(route: str, resource: Any, field: str) -> str:
    """The page named by one of a resource's own fields, or its generic detail page."""

    value = (resource.spec or {}).get(field)
    if value:
        try:
            return reverse(route, args=[value])
        except NoReverseMatch:
            pass
    return reverse("control_plane:detail", kwargs={"key": resource.key})


def key_from(text: Any) -> str:
    """A resource key from free text: lowercase, each run of anything else one hyphen."""

    return re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")
