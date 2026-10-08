"""Web → Domains: what each domain publishes, and how to change it.

Separate from the infrastructure views on purpose. The registry answers "which
declaration is wrong"; this answers "what does this domain say", which is the
question an operator actually arrives with and the one no per-resource page can
answer. Both read the same declarations: there is no second store and no
second truth, only a second way of slicing the first.
"""

from dataclasses import replace

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from hq.platform.application.routes import reverse
from django.utils.html import format_html
from django.views import View

from hq.platform.application.infrastructure import (
    NotFoundError,
    PolicyError,
    ManagedResourceCommand,
    save_managed_resource,
)
from hq.platform.application.pages import PageAction, page_context
from hq.platform.application.entity_links import entity_link
from hq.platform.application.relationships import relationships_for
from hq.platform.application.resource_capabilities import public_dns_enabled, resource_capabilities
from hq.platform.application.inventory import inventory_state
from hq.platform.application.adoption import AdoptCommand, adopt
from hq.platform.application.security import web_principal, safe_next

from .resource_form_views import _readable_error
from hq.platform.application.pins import DOMAIN, pinned, toggle
from hq.platform.application.mail_policy import (
    DMARC_TAGS,
    SPF_LOOKUP_LIMIT,
    SPF_DEFAULTS,
    SpfTerm,
    compose_dmarc,
    compose_spf,
    mail_overview,
    parse_spf,
)
from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.zones import (
    find_zone,
    RECORD_KIND,
    ZONE_KIND,
    adopt_zone_records,
    domain_context,
    zone_catalog,
)
from hq.platform.application.ui import counted
from hq.domains.control_plane.names import normalized_hostname


def _records_lede(zone) -> str:
    """Where this domain's records stand, as a clause beside the heading.

    Unmanaged records are not a backlog: a declared domain takes on its records
    with it, so anything left is genuinely new, added at the provider since.
    """

    if not zone.managed:
        return f"{counted(len(zone.records), 'record')}, none managed by HQ"
    if not zone.adoptable:
        return f"{counted(zone.managed_count, 'record')}, all managed by HQ"
    # Only ever seen in the gap between a record appearing at the provider and
    # the next sweep taking it on. Phrased as a statement of fact rather than
    # as a backlog, because it is not work anyone has to do.
    return (
        f"{counted(zone.managed_count, 'record')} managed by HQ, "
        f"{len(zone.adoptable)} new since the last read and picked up on the next"
    )


def _page_relationships(zone, relationships):
    """What a domain relates to, as its own page says it: each thing once.

    The cards at the head of the page already name the domain's services, so
    a relation whose every end is linked from a card is not said again. The
    connections the domain is read with are how HQ knows, not something the
    domain relates to, and are named apart, after the relations. The readings
    as they arrived are for the topology view, which the section links to.
    """

    carded = {
        url
        for card in zone.cards
        for url in (
            *(row.url for row in card.rows or ()),
            *(link.url for _label, links in card.links or () for link in links),
        )
        if url
    }
    kept, read_with = [], []
    for group in relationships.groups:
        ends = [item.entity for item in group.items]
        if ends and all(end.kind == "connection" for end in ends):
            read_with.extend(ends)
        elif not (carded and all(end.url in carded for end in ends)):
            kept.append(group)
    return replace(relationships, groups=tuple(kept), readouts=()), tuple(read_with)


class ZoneIndexView(View):
    """Straight to a domain when there is one to go to.

    A list page is a stop on the way to the page an operator actually wanted;
    the domain tabs already switch between them, so listing them again is a
    click that teaches nothing. Which domain this lands on is the operator's
    to decide: the catalog puts pinned domains first, so starring one makes
    it the one this opens.
    """

    def get(self, request):
        zones = zone_catalog(pinned=pinned(request.user, DOMAIN))
        managed = [zone for zone in zones if zone.managed]
        if managed:
            return redirect("zones:detail", zone=managed[0].zone)
        return render(
            request,
            "control_plane/zone_index.html",
            {
                "zones": zones,
                "inventory": inventory_state(),
                **page_context("Domains", "Cloudflare zones and their records."),
            },
        )


def _spf_default(value: str) -> str:
    """The qualifier on the policy's `all` term, which decides everyone unlisted."""

    for term in reversed(parse_spf(value).terms):
        if term.mechanism == "all":
            return term.qualifier
    return "-"


def _spf_value(zone) -> str:
    for record in zone.records:
        if record.record_type == "TXT" and "v=spf1" in record.content.lower():
            return record.content
    return ""


class ZoneMailView(View):
    """Everything that decides a domain's mail, on one page.

    Four records read separately mean nothing and read together are a policy:
    who receives, who may send, what signs, and what happens when a message
    proves none of it. The page is ordered the way mail actually flows.
    """

    def get(self, request, zone: str):
        found = find_zone(zone)
        if found is None:
            raise Http404("No such domain.")
        return render(
            request,
            "control_plane/zone_mail.html",
            {
                "zone": found,
                "mail": mail_overview(found),
                "policy_tags": DMARC_TAGS,
                "spf": parse_spf(_spf_value(found)),
                "spf_defaults": SPF_DEFAULTS,
                "spf_limit": SPF_LOOKUP_LIMIT,
                "spf_default": _spf_default(_spf_value(found)),
                **page_context(
                    f"Email for {found.zone}",
                    "Who receives mail for this domain, who may send as it, and what "
                    "happens to forgeries.",
                    trail=((found.zone, found.url),),
                ),
            },
        )

    def _publish(self, request, zone, record, value: str, what: str):
        """Write a composed policy back through the record's own use case."""

        if record is None or not record.resource_key:
            messages.error(request, f"HQ does not manage a {what} record for this domain.")
            return redirect("zones:mail", zone=zone.zone)
        resource = ManagedResource.objects.get(key=record.resource_key)
        try:
            save_managed_resource(
                ManagedResourceCommand(
                    key=resource.key,
                    kind=resource.kind,
                    spec={**resource.spec, "content": f'"{value}"'},
                    enabled=resource.enabled,
                ),
                principal=web_principal(request.user),
                current_key=resource.key,
            )
        except (DjangoValidationError, PolicyError, NotFoundError, ValueError) as exc:
            messages.error(request, _readable_error(exc))
            return redirect("zones:mail", zone=zone.zone)
        messages.success(
            request, f"{what} saved. Publishing within a minute."
        )
        return redirect("zones:mail", zone=zone.zone)

    def post(self, request, zone: str):
        """Publish a policy composed from the choices, not typed as a string."""

        found = find_zone(zone)
        if found is None:
            raise Http404("No such domain.")
        overview = mail_overview(found)

        if request.POST.get("section") == "spf":
            terms = []
            for rule in request.POST.getlist("rule"):
                rule = rule.strip()
                if not rule:
                    continue
                qualifier = rule[0] if rule[:1] in "+-~?" else "+"
                body = rule[1:] if rule[:1] in "+-~?" else rule
                mechanism, _, argument = body.partition(":")
                terms.append(SpfTerm(qualifier, mechanism.lower(), argument))
            terms.append(SpfTerm(request.POST.get("default", "-"), "all", ""))
            spf_record = next(
                (r for section in overview.sections if section.id == "sending"
                 for r in section.records),
                None,
            )
            return self._publish(
                request, found, spf_record, compose_spf(tuple(terms)), "SPF"
            )

        # Unknown tags survive: the record belongs to the operator, and an
        # editor that drops what it does not model deletes policy silently.
        tags = dict(overview.dmarc_tags)
        for tag in DMARC_TAGS:
            tags[tag.id] = request.POST.get(tag.id, "").strip()
        return self._publish(
            request, found, overview.dmarc_record, compose_dmarc(tags), "DMARC"
        )


class ZonePinView(View):
    """Star a domain so it sorts first, for this operator only."""

    def post(self, request, zone: str):
        name = normalized_hostname(zone)
        if not name:
            raise Http404("No such domain.")
        toggle(request.user, DOMAIN, name)
        # Checked, not trusted: a destination arriving in a form post is a
        # request, and an unchecked one redirects wherever it likes.
        return redirect(safe_next(request) or reverse("zones:index"))


def _pin_action(zone) -> PageAction:
    """Star a domain from its own head.

    Starring decides which domain `/domains/` opens: the catalog sorts pinned
    first, and that page goes to the first managed domain.
    """

    return PageAction(
        "★ Opens first" if zone.pinned else "☆ Open this one first",
        reverse("zones:pin", args=[zone.zone]),
        method="post",
        title=(
            "Domains opens on this one. Press to undo."
            if zone.pinned
            else "Make Domains open on this one."
        ),
    )


class ZoneDetailView(View):
    """One domain: every record in it, and what the zone currently says."""

    def get(self, request, zone):
        return self._page(request, zone)

    def _page(self, request, zone):
        context = domain_context(zone, pinned=pinned(request.user, DOMAIN))
        if context is None:
            raise Http404("No such domain.")
        found, zones = context.zone, context.zones
        relationships, read_with = _page_relationships(
            found, relationships_for(f"zone:{found.zone}", principal=web_principal(request.user))
        )
        return render(
            request,
            "control_plane/zone_detail.html",
            {
                "zone": found,
                # Every domain, so the switcher can reach an undeclared one
                # without going back to a list to find it.
                "zones": zones,
                "record_kind": RECORD_KIND,
                "inventory": inventory_state(),
                # A deployment can have changing public DNS switched off. Where
                # it is, every write below would be refused, and a page full of
                # buttons that always fail is worse than a page that says so
                # once.
                "public_dns_enabled": public_dns_enabled(),
                "records_lede": _records_lede(found),
                "relationships": relationships,
                "read_with": read_with,
                **page_context(
                    found.zone,
                    (
                        format_html(
                            'The DNS records of this domain, from <a href="{}" data-entity="{}">{}</a>.',
                            *_connection_mention(found.connection_ref),
                        )
                        if found.connection_ref
                        else "The DNS records of this domain."
                    ),
                    actions=(_pin_action(found), *_declaration_actions(found)),
                ),
            },
        )


def _declaration_actions(zone) -> tuple[PageAction, ...]:
    """Edit and removal for the domain's declaration, which lives on this page."""

    if not zone.resource_key:
        return ()
    resource = ManagedResource.objects.filter(key=zone.resource_key).first()
    if resource is None:
        return ()
    capabilities = resource_capabilities(resource)
    if capabilities.removal_pending:
        return ()
    actions = [PageAction("Edit domain", reverse("control_plane:edit", args=[resource.key]))]
    if capabilities.removal != "unavailable":
        actions.append(
            PageAction(
                "Stop tracking in HQ" if capabilities.removal == "forget" else "Remove",
                reverse("control_plane:remove", args=[resource.key]),
                danger=True,
                title=(
                    "HQ stops changing this domain. Its records stay live."
                    if capabilities.removal == "forget"
                    else ""
                ),
            )
        )
    return tuple(actions)


class ZoneAdoptView(View):
    """Take on a domain and everything published in it, exactly as it is.

    One action: declaring the domain is the decision. The records another
    system owns are the exception, and HQ settles that itself: an ACME challenge is working material HQ makes and
    clears up inside an issuance, not desired state, so it is never adopted
    and never listed: see ``zones.EPHEMERAL_PREFIXES``.
    """

    def post(self, request, zone):
        principal = web_principal(request.user)
        try:
            adopt(
                AdoptCommand(kind=ZONE_KIND, token=request.POST.get("token", "")),
                principal=principal,
            )
        except (NotFoundError, PolicyError, DjangoValidationError) as exc:
            messages.error(request, _readable_error(exc))
            return redirect("zones:detail", zone=zone)

        try:
            records = adopt_zone_records(zone, principal=principal)
            adopted = len(records["adopted"])
        except (NotFoundError, PolicyError, DjangoValidationError):
            # The domain is declared either way. A zone with nothing left to
            # take on is the ordinary case, not a failure worth interrupting.
            adopted = 0

        messages.success(
            request,
            f"HQ now manages {zone} and {counted(adopted, 'record')} in it. "
            "Nothing changed at Cloudflare.",
        )
        return redirect("zones:detail", zone=zone)


def _connection_mention(ref: str) -> tuple[str, str, str]:
    link = entity_link("connection", ref)
    return link.url, link.kind_label, link.label
