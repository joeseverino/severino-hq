"""
Documentation Index.

Severino HQ does NOT store full runbook bodies. The Obsidian vault remains the
source of truth. This model stores metadata and relationships only, so the
future severino-knowledge-router MCP can point an assistant at the right place.

No secrets should be stored here.
"""

from datetime import timedelta
from typing import override

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse

from hq.platform.application.ui import ended
from hq.platform.core.models import TimestampedModel
from hq.platform.core.rules import Rule
lazy from hq.platform.application.derivations import today


class DocumentationQuerySet(models.QuerySet):
    def needing_review(self):
        """Active docs past the configured review interval (or never reviewed).

        The one shared definition used by the dashboard, the docs list filter,
        and the reports page.

        Three doc types are excluded because they are not documentation about a
        system and so have no review cycle to fall behind. A public article
        draft belongs to the Content pipeline. A report is a frozen record, so
        reviewing it has nothing to clear. A queue note is generated from its
        items, so it is current whenever the tool writes it and reviewing it
        clears nothing. Each would put an item in the queue that reviewing
        cannot clear, which is the one thing a queue must never contain.
        """
        review_days = settings.SEVERINO_DOC_REVIEW_INTERVAL_DAYS

        cutoff = today() - timedelta(days=review_days)
        return self.filter(
            Q(last_reviewed__isnull=True) | Q(last_reviewed__lt=cutoff),
            status=self.model.Status.ACTIVE,
        ).exclude(
            doc_type__in=(
                self.model.DocType.PUBLIC_ARTICLE_DRAFT,
                self.model.DocType.REPORT,
                self.model.DocType.QUEUE,
            )
        )


class DocStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    DEPRECATED = "deprecated", "Deprecated"
    ARCHIVED = "archived", "Archived"


class DocumentationRecord(TimestampedModel):
    class DocType(models.TextChoices):
        RUNBOOK = "runbook", "Runbook"
        ARCHITECTURE_NOTE = "architecture_note", "Architecture note"
        DEPLOYMENT_GUIDE = "deployment_guide", "Deployment guide"
        TROUBLESHOOTING_GUIDE = "troubleshooting_guide", "Troubleshooting guide"
        RECOVERY_PROCEDURE = "recovery_procedure", "Recovery procedure"
        PUBLIC_ARTICLE_DRAFT = "public_article_draft", "Public article draft"
        DECISION_RECORD = "decision_record", "Decision record"
        REPORT = "report", "Report"
        QUEUE = "queue", "Queue"

    class Environment(models.TextChoices):
        HOMELAB = "homelab", "Homelab"
        VPS = "vps", "VPS"
        WORDPRESS = "wordpress", "WordPress"
        CLOUDFLARE = "cloudflare", "Cloudflare"
        TAILSCALE = "tailscale", "Tailscale"
        ADGUARD = "adguard", "AdGuard"
        UNIFI = "unifi", "UniFi"
        LOCAL_MAC = "local_mac", "Local Mac"
        OTHER = "other", "Other"

    # Declared above the model so the rule in ``Meta`` can name its values.
    Status = DocStatus

    class Sensitivity(models.TextChoices):
        PUBLIC = "public", "Public"
        INTERNAL = "internal", "Internal"
        SENSITIVE = "sensitive", "Sensitive"
        RESTRICTED = "restricted", "Restricted"

    doc_id = models.SlugField(
        max_length=80,
        unique=True,
        help_text=("Stable identifier, e.g. 'rb-adguard-001'. Never rename it."),
    )
    title = models.CharField(max_length=200)
    doc_type = models.CharField(max_length=32, choices=DocType.choices, default=DocType.RUNBOOK)
    system_service = models.CharField(
        max_length=120,
        blank=True,
        help_text="System or service the doc is about, e.g. 'AdGuard Home'.",
    )
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.OTHER,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    sensitivity = models.CharField(
        max_length=20,
        choices=Sensitivity.choices,
        default=Sensitivity.INTERNAL,
        help_text=("Public and internal docs are safe for AI export. Sensitive and restricted are not."),
    )

    obsidian_path = models.CharField(
        max_length=400,
        blank=True,
        help_text="Path inside the Obsidian vault, e.g. 'Infra/DNS/AdGuard Home.md'.",
    )
    github_path = models.CharField(
        max_length=400,
        blank=True,
        help_text="Repo-relative path if this doc also lives in a Git repo.",
    )
    external_url = models.URLField(blank=True)

    last_reviewed = models.DateField(null=True, blank=True)
    published_at = models.DateField(
        null=True,
        blank=True,
        help_text="Publication date for public articles. Empty for operational docs.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Index-level notes only. Do not paste runbook contents here.",
    )

    related_projects = models.ManyToManyField("projects.Project", blank=True, related_name="documentation_records")
    related_assets = models.ManyToManyField("assets.Asset", blank=True, related_name="documentation_records")
    related_expenses = models.ManyToManyField("expenses.Expense", blank=True, related_name="documentation_records")

    objects = DocumentationQuerySet.as_manager()

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("doc_type",)),
            models.Index(fields=("environment",)),
            models.Index(fields=("sensitivity",)),
            models.Index(fields=("last_reviewed",)),
        ]
        constraints = [
            Rule(
                condition=Q(status__in=DocStatus.values),
                name="documentation_status_is_a_doc_status",
                violation_error_message=ended(f"A doc's status is one of: {', '.join(DocStatus.values)}"),
                field="status",
            ),
        ]
        verbose_name = "Documentation record"

    @override
    def __str__(self) -> str:
        return f"{self.doc_id} · {self.title}"

    def get_absolute_url(self) -> str:
        return reverse("docs_index:detail", args=[self.doc_id])

    @property
    def is_safe_for_ai_export(self) -> bool:
        return self.sensitivity in SAFE_SENSITIVITIES


# Which sensitivities an AI-facing surface may name: the one definition, beside
# the choices it is made of. application.sensitivity serves it to every surface.
SAFE_SENSITIVITIES: tuple[str, ...] = (
    DocumentationRecord.Sensitivity.PUBLIC.value,
    DocumentationRecord.Sensitivity.INTERNAL.value,
)
