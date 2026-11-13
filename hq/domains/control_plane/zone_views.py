"""Web → Domains: what each domain publishes, and how to change it.

Separate from the infrastructure views on purpose. The registry answers "which
declaration is wrong"; this answers "what does this domain say", which is the
question an operator actually arrives with and the one no per-resource page can
answer. Both read the same declarations: there is no second store and no
second truth, only a second way of slicing the first.
"""

from dataclasses import replace

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.html import format_html
from django.views import View

from hq.domains.control_plane.forms import MailFailuresForm, MailPolicyForm, MailSendersForm
from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.names import normalized_hostname
from hq.platform.application.asks import APPLIES_SHORTLY
from hq.platform.application.capabilities import capability_words
from hq.platform.application.entity_links import entity_link
from hq.platform.application.inventory import inventory_state
from hq.platform.application.mail_policy import mail_overview, parse_spf
from hq.platform.application.pages import PageAction, page_context
from hq.platform.application.pins import DOMAIN, FAVORITE, FAVORITES, PIN, pinned
from hq.platform.application.relationships import relationships_for
from hq.platform.application.resource_capabilities import (
    REMOVAL_CAPABILITY,
    UPDATE_CAPABILITY,
    public_dns_enabled,
    resource_capabilities,
)
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.ui import counted
from hq.platform.application.web_writes import execute_posted, refused
from hq.platform.application.zones import (
    ADOPT_ZONE,
    RECORD_KIND,
    domain_context,
    find_zone,
    zone_catalog,
)
from hq.platform.core.templatetags.nav_tags import returning_to
from hq.platform.core.templatetags.value_tags import entity


def _records_lede(zone) -> str:
    """Where this domain's records stand, as a clause beside the heading.

    Unmanaged records are not a backlog: a declared domain takes on its records
    with it, so anything left is new at the provider since, and the next read
    takes it on.
    """

    if not zone.managed:
        return f"{counted(len(zone.records), 'record')}, none managed by HQ"
    if not zone.adoptable:
        return f"{counted(zone.managed_count, 'record')}, all managed by HQ"
    return f"{counted(zone.managed_count, 'record')} managed by HQ, {len(zone.adoptable)} new"


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

    page_reads = ("cloudflare.zone",)

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
                **page_context("Domains", reads=self.page_reads),
            },
        )


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
        return self._page(request, self._zone(zone))

    @staticmethod
    def _zone(zone: str):
        found = find_zone(zone)
        if found is None:
            raise Http404("No such domain.")
        return found

    def _page(self, request, found, sent: MailPolicyForm | None = None, status: int = 200):
        """The page, with the form ``sent`` drawn as it was sent over what was wrong with it."""

        overview = mail_overview(found)
        drawn = {
            MailSendersForm.SECTION: MailSendersForm(policy=parse_spf(_spf_value(found))),
            MailFailuresForm.SECTION: MailFailuresForm(tags=overview.dmarc_tags),
        }
        if sent is not None:
            drawn[sent.SECTION] = sent
        return render(
            request,
            "control_plane/zone_mail.html",
            {
                "zone": found,
                "mail": overview,
                "senders_form": drawn[MailSendersForm.SECTION],
                "failures_form": drawn[MailFailuresForm.SECTION],
                # The form a refusal is answered over.
                "form": sent,
                **page_context(
                    f"Email for {found.zone}",
                    trail=((found.zone, found.url),),
                ),
            },
            status=status,
        )

    def post(self, request, zone: str):
        """Publish a policy composed from the choices, not typed as a string."""

        found = self._zone(zone)
        overview = mail_overview(found)
        form: MailPolicyForm
        if request.POST.get("section") == MailSendersForm.SECTION:
            form = MailSendersForm(request.POST, policy=parse_spf(_spf_value(found)))
            record = next(
                (r for section in overview.sections if section.id == "sending" for r in section.records),
                None,
            )
        else:
            form = MailFailuresForm(request.POST, tags=overview.dmarc_tags)
            record = overview.dmarc_record
        if not form.is_valid():
            return self._page(request, found, form, status=400)
        if record is None or not record.resource_key:
            form.add_error(None, f"HQ does not manage a {form.record} record for this domain.")
            return self._page(request, found, form, status=409)
        resource = ManagedResource.objects.get(key=record.resource_key)
        result = execute_posted(
            request,
            "infrastructure.resource.update",
            {
                "key": resource.key,
                "kind": resource.kind,
                "spec": {**resource.spec, "content": f'"{form.published()}"'},
                "enabled": resource.enabled,
            },
            target=resource.key,
        )
        if refusal := refused(request, result, form=form):
            return self._page(request, found, form, status=refusal.status)
        messages.success(request, f"{form.record} saved. {APPLIES_SHORTLY}")
        return redirect("zones:mail", zone=found.zone)


class ZonePinView(View):
    """Star a domain so it sorts first, for this operator only."""

    def post(self, request, zone: str):
        name = normalized_hostname(zone)
        if not name:
            raise Http404("No such domain.")
        refused(request, execute_posted(request, PIN, {"kind": DOMAIN, "key": name}))
        # Checked, not trusted: a destination arriving in a form post is a
        # request, and an unchecked one redirects wherever it likes.
        return redirect(safe_next(request) or reverse("zones:index"))


def _pin_action(zone) -> PageAction:
    """Star a domain from its own head.

    Starring decides which domain `/domains/` opens: the catalog sorts pinned
    first, and that page goes to the first managed domain.
    """

    return PageAction(
        f"★ {FAVORITE}" if zone.pinned else f"☆ Add to {FAVORITES}",
        reverse("zones:pin", args=[zone.zone]),
        method="post",
        title=(
            f"Domains opens on your first {FAVORITE}. Press to remove this one from {FAVORITES}."
            if zone.pinned
            else f"Domains opens on your first {FAVORITE}."
        ),
    )


class ZoneDetailView(View):
    """One domain: every record in it, and what the zone currently says."""

    def get(self, request, zone):
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
                "record_rows": {
                    "routing": _record_rows(found.routing, request.get_full_path()),
                    "policy": _record_rows(found.policy, request.get_full_path()),
                },
                "relationships": relationships,
                "read_with": read_with,
                **page_context(
                    found.zone,
                    (
                        format_html(
                            "Records from {}.",
                            entity({"request": request}, entity_link("connection", found.connection_ref)),
                        )
                        if found.connection_ref
                        else ""
                    ),
                    actions=(_pin_action(found), *_declaration_actions(found)),
                ),
            },
        )


def _record_actions(record, here: str) -> tuple[PageAction, ...]:
    """What a record's declaration offers from its row: Edit, which comes back
    to this page, and Remove."""

    actions = []
    if record.edit_url:
        actions.append(PageAction(capability_words(UPDATE_CAPABILITY), returning_to(record.edit_url, here)))
    if record.remove_url:
        actions.append(PageAction(capability_words(REMOVAL_CAPABILITY), record.remove_url, danger=True))
    return tuple(actions)


def _record_rows(records, here: str) -> list[tuple]:
    """Each record with its row's actions, as ``_zone_records.html`` draws them."""

    return [(record, _record_actions(record, here)) for record in records]


def _declaration_actions(zone) -> tuple[PageAction, ...]:
    """Edit and removal for the domain's declaration, which lives on this page."""

    if not zone.resource_key:
        return ()
    resource = ManagedResource.objects.filter(key=zone.resource_key).first()
    if resource is None:
        return ()
    capabilities = resource_capabilities(resource)
    offered = capabilities.offered
    actions = [PageAction(capability_words(UPDATE_CAPABILITY), offered.edit_url)] if offered.edit_url else []
    if offered.remove_url:
        actions.append(
            PageAction(
                capabilities.removal_words,
                offered.remove_url,
                danger=True,
                title=(
                    "HQ stops changing this domain. Its records stay live." if capabilities.removal == "forget" else ""
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
        result = execute_posted(request, ADOPT_ZONE, {"zone": zone, "token": request.POST.get("token", "")})
        if refused(request, result):
            return redirect("zones:detail", zone=zone)
        adopted = len(result["adopted"])

        messages.success(
            request,
            f"HQ now manages {zone} and {counted(adopted, 'record')} in it. Nothing changed at Cloudflare.",
        )
        return redirect("zones:detail", zone=zone)
