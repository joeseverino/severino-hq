"""Infrastructure findings."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from application.routes import reverse
from django.views.generic import TemplateView

from application.findings import derive_findings, finding_layout, finding_rules, rule_for
from application.topology import derive_topology
from application.security import web_principal
from application.pages import PageAction, PageMixin


class FindingsView(PageMixin, LoginRequiredMixin, TemplateView):
    """Evidence and safe existing actions for claims from the live topology."""

    template_name = "control_plane/findings.html"
    page_title = "Findings"

    def get_page_actions(self):
        return (
            PageAction("Action items", reverse("action_items")),
            PageAction("Topology", reverse("control_plane:topology")),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        principal = web_principal(self.request.user)
        topology = derive_topology(principal=principal)
        requested_rule = self.request.GET.get("rule", "").strip()
        active_rule = rule_for(requested_rule)
        raised = derive_findings(
            topology,
            principal=principal,
            rule=active_rule.name if active_rule else "",
        )
        entries = []
        for finding in raised:
            entries.append(
                {
                    "finding": finding,
                    "workflow": finding.workflow,
                    "layout": finding_layout(finding),
                }
            )

        counts: dict[str, int] = {}
        for finding in raised:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        context.update(
            {
                "finding_entries": tuple(entries),
                # Only rules that raised something, or the one being viewed. A
                # chip for every rule HQ knows is a list of what is fine.
                "finding_rules": tuple(
                    rule
                    for rule in finding_rules()
                    if rule.name in {finding.rule for finding in raised}
                    or (active_rule and rule.name == active_rule.name)
                ),
                "active_rule": active_rule,
                "finding_counts": counts,
            }
        )
        return context
