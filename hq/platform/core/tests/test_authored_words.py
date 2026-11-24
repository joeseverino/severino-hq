"""What a person wrote outside HQ (a playbook's author, a vault doc's, a
writeup's, a starred repository's owner) is shown as written, escaped, on
every page that draws it, however much it reads like markup or a stamp; and
so is an audit row's stored message."""

from typing import Any
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import ApprovalRequest, OperationRequest, ProviderInventory, SourceProposal
from hq.domains.control_plane.observations.github import PROFILE_KIND
from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.application.markup_testing import text
from hq.platform.core import seeds
from hq.platform.core.example_estate import automation
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.models import AuditLog

# What an author may well write: a placeholder, an attribute, a time.
MARKED = '<revision> a="b" 2026-11-24 10:00'


def _said(texts: list[str]) -> list[str]:
    """Each of ``texts`` carrying ``MARKED``, and one more of it alone, so a
    page joins two or more of an author's statements into one value."""

    return [*(f"{each} {MARKED}" for each in texts), MARKED] if texts else []


def _marked(procedure: dict[str, Any]) -> dict[str, Any]:
    """``procedure`` with every text its author wrote, and each problem its
    scan found, carrying ``MARKED``."""

    roles = [
        {
            **role,
            "argument_spec": {
                **role["argument_spec"],
                "short_description": f"{role['argument_spec']['short_description']} {MARKED}",
                "options": {
                    name: {**terms, "description": f"{terms['description']} {MARKED}"}
                    for name, terms in role["argument_spec"]["options"].items()
                },
            },
        }
        for role in procedure["roles"]
    ]
    return {
        **procedure,
        "title": f"{procedure['title']} {MARKED}",
        "preconditions": _said(procedure["preconditions"]),
        "verification": _said(procedure["verification"]),
        "declaration": {**procedure["declaration"], "recovery": _said(procedure["declaration"]["recovery"])},
        "roles": roles,
        "problems": _said(procedure["problems"]),
    }


class AuthoredWordsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        scanned = automation.catalog
        # Every commit the estate's worker reads, as an author who writes so.
        with (
            mock.patch.object(
                automation,
                "catalog",
                lambda procedures=automation.PROCEDURES, *rest: scanned(tuple(map(_marked, procedures)), *rest),
            ),
            throwaway_media(),
            seeds.built("showcase") as owner,
        ):
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)

    def _shows(self, url: str, *, names: bool = True) -> None:
        """``url`` draws, with no author's word as markup, and ``names`` one
        of the playbooks by its author's words."""

        with self.subTest(url=url):
            response = self.client.get(url, follow=True)
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertNotIn("<revision>", html)
            if names:
                self.assertIn(MARKED, text(html))

    def test_every_page_drawing_a_playbook_shows_its_authors_words_escaped(self):
        from hq.platform.application.automation_sources import entry_url
        from hq.platform.application.entity_links import entity_link
        from hq.platform.application.work_runs import run_url

        runs = OperationRequest.objects.filter(action=OperationRequest.Action.RUN)
        held = ApprovalRequest.objects.filter(state="pending", capability__startswith="automation.")
        for url in (
            reverse("command", args=[f"automation.{automation.CACHE_FLUSH}.run"]),
            reverse("control_plane:playbooks"),
            reverse("jobs:list"),
            entity_link("machine", MACHINE).url,
            *(run_url(str(run.pk)) for run in runs),
        ):
            self._shows(url)
        # A held run, and each scan's read, which names the playbooks it changes or none.
        for url in (
            *(reverse("core:approval_entry", kwargs={"approval_id": each.pk}) for each in held),
            *(entry_url(proposal) for proposal in SourceProposal.objects.all()),
        ):
            self._shows(url, names=False)

    def test_every_other_outside_authors_words_and_a_stored_audit_message_show_escaped(self):
        doc = DocumentationRecord.objects.order_by("pk").first()
        DocumentationRecord.objects.filter(pk=doc.pk).update(title=f"Runbook {MARKED}")
        ContentItem.objects.filter(pk=ContentItem.objects.order_by("pk").first().pk).update(
            title=f"Writeup {MARKED}", topic=f"About {MARKED}"
        )
        reading = ProviderInventory.objects.get(kind=PROFILE_KIND)
        for record in reading.records:
            for repo in record.get("watched", []):
                repo["description"] = f"A tool {MARKED}"
                repo["advisories"] = [
                    {"id": "GHSA-0000-0000-0000", "url": "https://example.com/advisory", "summary": MARKED}
                ]
        reading.save()
        AuditLog.objects.create(action=AuditLog.Action.UPDATED, object_type="auth.user", message=MARKED)
        for url in (
            reverse("docs_index:list"),
            reverse("docs_index:detail", args=[doc.doc_id]),
            reverse("content:list"),
            reverse("watching"),
            reverse("core:audit_list"),
        ):
            self._shows(url)
