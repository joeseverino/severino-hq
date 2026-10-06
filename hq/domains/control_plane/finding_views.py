"""Infrastructure findings."""

from __future__ import annotations

from hq.platform.application.routes import reverse
from django.views.generic import TemplateView

from hq.platform.application.findings import estate_findings, finding_layout, finding_rules, rule_for
from hq.platform.application.security import web_principal
from hq.platform.application.pages import PageAction, PageMixin


class FindingsView(PageMixin, TemplateView):
    """Evidence and safe existing actions for claims from the live topology."""

    template_name = "control_plane/findings.html"
    page_title = "Findings"

    def get_page_actions(self):
        return (
            PageAction("Needs you", reverse("action_items")),
            PageAction("Topology", reverse("control_plane:topology")),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        principal = web_principal(self.request.user)
        requested_rule = self.request.GET.get("rule", "").strip()
        active_rule = rule_for(requested_rule)
        raised = estate_findings(
            principal=principal, rule=active_rule.name if active_rule else ""
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
