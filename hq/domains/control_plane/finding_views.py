"""Infrastructure findings."""

from typing import override

from django.views.generic import TemplateView

from hq.platform.application.attention import finding_insight
from hq.platform.application.dashboard import queue_item, waiting
from hq.platform.application.findings import coverage, estate_findings, finding_rules, rule_for
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal

# The queue's source for findings: the domain whose pages they are about.
SOURCE_ID, SOURCE = "hq.infrastructure", "Infrastructure"


class FindingsView(PageMixin, TemplateView):
    """Evidence and safe existing actions for claims from the live topology."""

    template_name = "control_plane/findings.html"
    page_title = "Findings"

    @override
    def get_page_actions(self):
        return (
            PageAction("Needs you", reverse("action_items")),
            PageAction("Map", reverse("control_plane:topology")),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        principal = web_principal(self.request.user)
        requested_rule = self.request.GET.get("rule", "").strip()
        active_rule = rule_for(requested_rule)
        raised = estate_findings(principal=principal, rule=active_rule.name if active_rule else "")
        url = reverse("control_plane:findings")
        # The queue's own cards: one renderer for a problem wherever it is drawn.
        entries = [queue_item(SOURCE_ID, SOURCE, finding_insight(finding, url)) for finding in raised]

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
                # How far a page with none goes: a clean page over nothing read is not clean.
                "finding_coverage": coverage() if not raised and not active_rule else None,
                # The checks are one source of the queue; what else waits is
                # named beside their verdict, so the two pages never disagree.
                "waiting_elsewhere": waiting(self.request.user.pk) if not raised and not active_rule else 0,
            }
        )
        return context
