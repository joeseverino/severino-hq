"""Web → Domains: what each domain publishes, and how to change it.

Separate from the infrastructure views on purpose. The registry answers "which
declaration is wrong"; this answers "what does this domain say", which is the
question an operator actually arrives with and the one no per-resource page can
answer. Both read the same declarations: there is no second store and no
second truth, only a second way of slicing the first.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from functools import cached_property
from typing import override

from django.http import Http404
from django.shortcuts import redirect, render
from django.views import View
from django.views.generic import TemplateView

from hq.domains.control_plane.forms import MailFailuresForm, MailPolicyForm, MailSendersForm
from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.entity_links import entity_link, page_url
from hq.platform.application.inventory import inventory_state
from hq.platform.application.mail_policy import mail_overview, parse_spf
from hq.platform.application.pages import PageAction, RecordPageMixin, page_context
from hq.platform.application.pins import DOMAIN, PIN, pinned
from hq.platform.application.problems import About, problems_of
from hq.platform.application.references import Kind
from hq.platform.application.relationships import relationships_for
from hq.platform.application.resource_capabilities import (
    MAIL_POLICY,
    REMOVAL_CAPABILITY,
    UPDATE_CAPABILITY,
    Offered,
    declaration_actions,
    public_dns_enabled,
)
from hq.platform.application.resources import every_item
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.tables import Cell, DataTable, Group, Row, StatusMark, TableColumn
from hq.platform.application.tone import Absent
from hq.platform.application.ui import counted, measured
from hq.platform.application.web_writes import execute_posted
from hq.platform.application.zone_findings import MAIL_CARD
from hq.platform.application.zones import ADOPT_ZONE, RECORD_KIND, find_zone, zone_catalog
from hq.platform.core.held_values import format_html
from hq.platform.core.templatetags.value_tags import entity
from hq.platform.core.web_refusals import refused


def _records_lede(domain) -> str:
    """Where this domain's records stand, as a clause beside the heading.

    Unmanaged records are not a backlog: a declared domain takes on its records
    with it, so anything left is new at the provider since, and the next read
    takes it on.
    """

    records = domain["records"]
    if not domain["declaration"]:
        return f"{counted(len(records), 'record')}, none managed by HQ"
    managed = sum(1 for record in records if record["declaration"])
    new = sum(
        1 for record in records if not record["declaration"] and record["manageable"] and not record["observed_only"]
    )
    if not new:
        return f"{counted(managed, 'record')}, all managed by HQ"
    return f"{counted(managed, 'record')} managed by HQ, {new} new"


def _page_relationships(domain, relationships):
    """What a domain relates to, as its own page says it: each thing once.

    The cards at the head of the page already name the domain's services, so
    a relation whose every end is linked from a card is not said again. The
    connections the domain is read with are how HQ knows, not something the
    domain relates to, and are named apart, after the relations. The readings
    as they arrived are for the topology view, which the section links to.
    """

    carded = {
        url
        for insight in domain["insights"]
        for url in (
            *(each["url"] for each in insight["rows"]),
            *(link["url"] for group in insight["links"] for link in group["links"]),
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
            return redirect(page_url(Kind.ZONE, managed[0].zone))
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

    runs = ((UPDATE_CAPABILITY, MAIL_POLICY),)

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
        drawn = {section: _policy(section, found, overview)[0] for section in _SECTIONS}
        if sent is not None:
            drawn[sent.SECTION] = sent
        return render(
            request,
            "control_plane/zone_mail.html",
            {
                "zone": found,
                "mail": overview,
                # What is wrong with its mail, from the findings about the domain.
                "problems": problems_of(About(urls=(found.url,), cards=(MAIL_CARD,)), web_principal(request.user)),
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
        section = request.POST.get("section")
        form, resource = _policy(
            section if section in _SECTIONS else MailFailuresForm.SECTION, found, mail_overview(found), request.POST
        )
        if not form.is_valid():
            return self._page(request, found, form, status=400)
        if resource is None:
            form.add_error(None, f"HQ does not manage a {form.record} record for this domain.")
            return self._page(request, found, form, status=409)
        result = execute_posted(
            request,
            UPDATE_CAPABILITY,
            {
                "key": resource.key,
                "kind": resource.kind,
                "spec": {**resource.spec, "content": f'"{form.published()}"'},
                "enabled": resource.enabled,
            },
            target=resource.key,
            expected_updated_at=form.cleaned_data["expected_updated_at"] or None,
        )
        if refusal := refused(request, result, form=form):
            return self._page(request, found, form, status=refusal.status)
        return redirect("zones:mail", zone=found.zone)


_SECTIONS = (MailSendersForm.SECTION, MailFailuresForm.SECTION)


def _policy(section: str, found, overview, data=None) -> tuple[MailPolicyForm, ManagedResource | None]:
    """One policy's form, drawn from the record it rewrites, and that record when HQ manages it."""

    form: type[MailPolicyForm]
    if section == MailSendersForm.SECTION:
        record = next((r for part in overview.sections if part.id == "sending" for r in part.records), None)
        form, drawn_from = MailSendersForm, {"policy": parse_spf(_spf_value(found))}
    else:
        record = overview.dmarc_record
        form, drawn_from = MailFailuresForm, {"tags": overview.dmarc_tags}
    resource = (
        ManagedResource.objects.filter(key=record.resource_key).first() if record and record.resource_key else None
    )
    initial = {"expected_updated_at": resource.updated_at.isoformat()} if resource else {}
    return form(data, initial=initial, **drawn_from), resource


class ZonePinView(View):
    """Star a domain so it sorts first, for this operator only."""

    runs = (PIN,)

    def post(self, request, zone: str):
        refused(request, execute_posted(request, PIN, {"kind": DOMAIN, "key": zone}))
        # Checked, not trusted: a destination arriving in a form post is a
        # request, and an unchecked one redirects wherever it likes.
        return redirect(safe_next(request, fallback=reverse("zones:index")))


def _pin_action(name: str, pinned: bool) -> PageAction | None:
    """Star a domain from its own head.

    Starring decides which domain `/domains/` opens: the catalog sorts pinned
    first, and that page goes to the first managed domain.
    """

    return PageAction.command(PIN, name, variant="off" if pinned else "on")


class ZoneDetailView(RecordPageMixin, TemplateView):
    """One domain: every record in it, and what the zone currently says."""

    template_name = "control_plane/zone_detail.html"
    resource = "domains"
    context_object_name = "domain"
    url_kwarg = "zone"

    @cached_property
    def pins(self) -> frozenset[str]:
        return pinned(self.request.user, DOMAIN)

    @override
    def get_page_lede(self):
        ref = self.record["connection_ref"]
        if not ref:
            return ""
        return format_html("Records from {}.", entity({"request": self.request}, entity_link("connection", ref)))

    @override
    def get_page_actions(self):
        name = self.record["name"]
        return (_pin_action(name, name in self.pins), *declaration_actions(self.record["declaration"]))

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        domain = self.record
        name = domain["name"]
        here = self.request.get_full_path()
        relationships, read_with = _page_relationships(
            domain, relationships_for(Kind.ZONE.of(name), principal=web_principal(self.request.user))
        )
        rows = [(record, _record_actions(record, here)) for record in domain["records"]]
        every = every_item("domains", principal=web_principal(self.request.user), fields=["name", "url", "declaration"])
        context.update(
            {
                # Every domain, so the switcher can reach an undeclared one
                # without going back to a list to find it: pinned first, as
                # the catalog sorts them.
                "zones": [
                    {
                        "url": each["url"],
                        "zone": each["name"],
                        "pinned": each["name"] in self.pins,
                        "managed": bool(each["declaration"]),
                        "current": each["name"] == name,
                    }
                    for each in sorted(every, key=lambda each: (each["name"] not in self.pins, each["name"]))
                ],
                "adopt": (
                    PageAction.post(ADOPT_ZONE, name, sends={"token": domain["adoption"]}, primary=True)
                    if domain["adoption"]
                    else None
                ),
                "record_kind": RECORD_KIND,
                "inventory": inventory_state(),
                # A deployment can have changing public DNS switched off. Where
                # it is, every write below would be refused, and a page full of
                # buttons that always fail is worse than a page that says so
                # once.
                "public_dns_enabled": public_dns_enabled(),
                "records_lede": _records_lede(domain),
                "records_table": records_table(
                    [(record, actions) for record, actions in rows if not record["secondary"]],
                    [(record, actions) for record, actions in rows if record["secondary"]],
                    managed=bool(domain["declaration"]),
                ),
                "relationships": relationships,
                "read_with": read_with,
            }
        )
        return context


# The fold the policy and verification records start in.
_POLICY_FOLD = "zone-policy-records"


def records_table(
    routing: Sequence[tuple[Mapping, tuple[PageAction, ...]]],
    policy: Sequence[tuple[Mapping, tuple[PageAction, ...]]],
    *,
    managed: bool,
) -> DataTable:
    """A zone's DNS records as one table: those that send traffic somewhere,
    then the policy and verification records folded away under their own
    group, both sharing one set of columns. A state is said only where it is
    not healthy, and a TTL only where it was set: 1 leaves it to the DNS
    service, so a zone where every record does has no TTL column. Where the
    Value column has left, a record that shares its name and type with another
    says its value under its name. Unmanaged, a zone offers no State or row
    actions."""

    ttl_set = any(record["ttl"] != 1 for record, _actions in (*routing, *policy))
    columns = (
        TableColumn("Name", kind="identifier"),
        TableColumn("Type", kind="identifier", css="key-col"),
        TableColumn("Value", kind="identifier", drop=4),
        *((TableColumn("TTL", kind="status", drop=3),) if ttl_set else ()),
        *((TableColumn("State", kind="status"),) if managed else ()),
    )

    def rows(found, folded=""):
        return tuple(
            Row(
                _record_cells(record, ttl_set=ttl_set, managed=managed),
                actions=actions if managed else (),
                folded=folded,
            )
            for record, actions in found
        )

    region = {"empty": "No records."}
    if not policy:
        return DataTable("Records", columns, rows(routing), region=region)
    return DataTable(
        "Records",
        columns,
        groups=(
            *((Group("Routing records", rows(routing)),) if routing else ()),
            Group("Policy records", rows(policy, _POLICY_FOLD), folds=_POLICY_FOLD),
        ),
        region=region,
    )


def _record_cells(record: Mapping, *, ttl_set: bool, managed: bool) -> tuple[Cell, ...]:
    summary = record.get("summary")
    name = Cell.of(entity_link("service", record["service"])) if record.get("service") else Cell(record["name"])
    proxied = (Cell(status=StatusMark("Proxied", tone="neutral")),) if record.get("proxied") else ()
    return (
        replace(name, lines=(Cell(summary, muted=True, role="data", shows="narrow"),) if summary else ()),
        Cell(record["record_type"], readout=True),
        Cell(record["value"], readout=True, parts=proxied),
        *((Cell(measured(record["ttl"], "s") if record["ttl"] != 1 else ""),) if ttl_set else ()),
        *((_record_state(record),) if managed else ()),
    )


def _record_state(record: Mapping) -> Cell:
    """A type HQ does not model is still a real record in the zone, so it is
    shown, and said to be outside HQ's reach, rather than labeled New."""

    if not record.get("manageable"):
        return Cell(status=StatusMark(record["record_type"], Absent.NOT_MANAGED))
    if not record.get("declaration"):
        return Cell("New", tone="attention")
    health = record["health"]
    return Cell(health["label"], tone=health["state"]) if health["state"] != "healthy" else Cell()


def _record_actions(record, here: str) -> tuple[PageAction, ...]:
    """What a record's declaration offers from its row: Edit, which comes back
    to this page, and Remove."""

    key = record["declaration"]
    offered = (
        Offered(edits=record["edits"]).edit(key, next=here),
        PageAction.command(REMOVAL_CAPABILITY, key) if record["removes"] else None,
    )
    return tuple(filter(None, offered))


class ZoneAdoptView(View):
    """Take on a domain and everything published in it, exactly as it is.

    One action: declaring the domain is the decision. The records another
    system owns are the exception, and HQ settles that itself: an ACME challenge is working material HQ makes and
    clears up inside an issuance, not desired state, so it is never adopted
    and never listed: see ``zones.EPHEMERAL_PREFIXES``.
    """

    runs = (ADOPT_ZONE,)

    def post(self, request, zone):
        refused(request, execute_posted(request, ADOPT_ZONE, {"zone": zone, "token": request.POST.get("token", "")}))
        return redirect(page_url(Kind.ZONE, zone))
