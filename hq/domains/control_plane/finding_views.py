"""Infrastructure findings."""

from collections import Counter
from functools import cached_property
from typing import override

from django.urls import reverse_lazy
from django.views.generic import TemplateView

from hq.platform.application.dashboard import finding_rows, waiting
from hq.platform.application.finding_checks import checks, tones
from hq.platform.application.findings import coverage, estate_findings, finding_rules, rule_for
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
from hq.platform.core import queue_table
from hq.platform.core.templatetags.status_tags import urgency_tone

# The queue's source for findings: the domain whose pages they are about.
SOURCE_ID, SOURCE = "hq.infrastructure", "Infrastructure"


class FindingsView(PageMixin, TemplateView):
    """Evidence and safe existing actions for claims from the live topology."""

    template_name = "control_plane/findings.html"
    page_title = "Findings"

    @cached_property
    def _shown(self):
        """Every problem, those of the rule viewed, and those as the queue's rows."""

        principal = web_principal(self.request.user)
        active_rule = rule_for(self.request.GET.get("rule", "").strip())
        # Every problem, so the bar counts the shown ones out of all of them
        # and offers each rule that raised one, whichever is viewed.
        every = estate_findings(principal=principal)
        raised = tuple(finding for finding in every if not active_rule or finding.rule == active_rule.name)
        # The queue's own cards: one renderer for a problem wherever it is drawn.
        return principal, active_rule, every, raised, finding_rows(SOURCE_ID, SOURCE, raised)

    @override
    def get_page_actions(self):
        principal, *_, entries = self._shown
        refresh = queue_table.refresh(entries, principal, self.request.get_full_path())
        return (
            *((refresh,) if refresh else ()),
            PageAction("Needs you", reverse("action_items")),
            PageAction("Map", reverse("control_plane:topology")),
            PageAction("Checks", reverse("control_plane:checks")),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        principal, active_rule, every, raised, entries = self._shown

        # Counted by the tone each row's urgency is drawn in, so a notice
        # counts as the note it reads as.
        counts = Counter(urgency_tone(entry) for entry in entries)
        context.update(
            {
                "finding_entries": entries,
                "finding_total": len(every),
                # Only rules that raised something, or the one being viewed. A
                # chip for every rule HQ knows is a list of what is fine.
                "finding_rules": tuple(
                    (rule.name, rule.title)
                    for rule in finding_rules()
                    if rule.name in {finding.rule for finding in every}
                    or (active_rule and rule.name == active_rule.name)
                ),
                "active_rule": active_rule,
                "finding_counts": counts,
                # How far a page with none goes: a clean page over nothing read is not clean.
                "finding_coverage": coverage() if not raised and not active_rule else None,
                # The checks are one source of the queue; what else waits is
                # named beside their verdict, so the two pages never disagree.
                "waiting_elsewhere": waiting(principal, self.request.user.pk) if not raised and not active_rule else 0,
            }
        )
        return context


class ChecksView(PageMixin, TemplateView):
    """Every rule the findings come from, with its bars and how many are open."""

    template_name = "control_plane/checks.html"
    page_title = "Checks"
    # Below the findings it explains, whose own action opens it.
    page_trail = (("Findings", reverse_lazy("control_plane:findings")),)

    @override
    def get_page_lede(self):
        return tones()

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["checks"] = checks(web_principal(self.request.user))
        return context
