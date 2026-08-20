"""Web → Domains: what each domain publishes, and how to change it.

Separate from the infrastructure views on purpose. The registry answers "which
declaration is wrong"; this answers "what does this domain say", which is the
question an operator actually arrives with and the one no per-resource page can
answer. Both read the same declarations -- there is no second store and no
second truth, only a second way of slicing the first.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from application.infrastructure import NotFoundError, PolicyError
from application.inventory import AdoptCommand, adopt, inventory_state
from application.security import web_principal

from .views import _readable_error
from application.pins import DOMAIN, pinned, toggle
from application.mail_policy import (
    DMARC_TAGS,
    SPF_LOOKUP_LIMIT,
    SPF_DEFAULTS,
    SpfTerm,
    compose_dmarc,
    compose_spf,
    mail_overview,
    parse_spf,
)
from application.infrastructure import (
    ManagedResourceCommand,
    save_managed_resource,
)
from control_plane.models import ManagedResource
from application.zones import (
    find_zone,
    RECORD_KIND,
    ZONE_KIND,
    adopt_zone_records,
    zone_catalog,
)
from control_plane.providers import normalized_hostname


def _records_lede(zone) -> str:
    """One line saying where this domain stands, in the operator's terms.

    "0 managed by HQ, 17 not yet" described a backlog that was never work: a
    declared domain takes on its records with it, so anything left is genuinely
    new -- added at the provider since.
    """

    if not zone.managed:
        return f"{len(zone.records)} published. HQ manages none of them yet."
    if not zone.adoptable:
        # "Managed" conflated two things: that HQ holds a declaration, and that
        # the declaration has been applied. The State column already says which
        # records have been observed, so this says the first and only the first.
        return f"All {zone.managed_count} declared in HQ."
    # Only ever seen in the gap between a record appearing at the provider and
    # the next sweep taking it on. Phrased as a statement of fact rather than
    # as a backlog, because it is not work anyone has to do.
    return (
        f"{zone.managed_count} declared in HQ. "
        f"{len(zone.adoptable)} found since the last sweep, taken on shortly."
    )


class ZoneIndexView(LoginRequiredMixin, View):
    """Straight to a domain when there is one to go to.

    A list page is a stop on the way to the page an operator actually wanted;
    the domain tabs already switch between them, so listing them again is a
    click that teaches nothing. Which domain this lands on is the operator's
    to decide -- the catalog puts pinned domains first, so starring one makes
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
            {"zones": zones, "inventory": inventory_state()},
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


class ZoneMailView(LoginRequiredMixin, View):
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
            },
        )

    def _publish(self, request, zone, record, value: str, what: str):
        """Write a composed policy back through the record's own use case."""

        if record is None or not record.resource_key:
            messages.error(request, f"No {what} record is declared here yet.")
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
            request, f"{what} saved. HQ publishes it within about a minute."
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


class ZonePinView(LoginRequiredMixin, View):
    """Star a domain so it sorts first, for this operator only."""

    def post(self, request, zone: str):
        name = normalized_hostname(zone)
        if not name:
            raise Http404("No such domain.")
        toggle(request.user, DOMAIN, name)
        return redirect(request.POST.get("next") or reverse("zones:index"))


class ZoneDetailView(LoginRequiredMixin, View):
    """One domain: every record in it, and what the zone currently says."""

    def get(self, request, zone):
        # Built once and searched, rather than built to find one and built again
        # for the switcher. Each build reads every declaration, every stored
        # sweep and the whole unmanaged diff, so doing it twice doubled the cost
        # of the page to produce two identical answers.
        zones = zone_catalog(pinned=pinned(request.user, DOMAIN))
        wanted = normalized_hostname(zone)
        found = next((item for item in zones if item.zone == wanted), None)
        if found is None:
            raise Http404("No such domain.")
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
                "public_dns_enabled": getattr(
                    settings, "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False
                ),
                "records_lede": _records_lede(found),
            },
        )


class ZoneAdoptView(LoginRequiredMixin, View):
    """Take on a domain and everything published in it, exactly as it is.

    One action, because there was never a second decision in it. Asked per
    record, the page put seventeen Adopt buttons in front of an operator who
    had just said this domain was theirs, and every one of them had the same
    answer. The records another system owns are the real exception, and HQ
    settles that itself: an ACME challenge is working material HQ makes and
    clears up inside an issuance, not desired state, so it is never adopted
    and never listed -- see ``zones.EPHEMERAL_PREFIXES``.
    """

    def post(self, request, zone):
        principal = web_principal(request.user)
        try:
            result = adopt(
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
            f"{zone} is managed by HQ as “{result['resource']['key']}”, with "
            f"{adopted} record{'' if adopted == 1 else 's'} exactly as "
            "configured now. Nothing changed at Cloudflare.",
        )
        return redirect("zones:detail", zone=zone)
