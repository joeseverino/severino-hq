"""Documentation Index: the vault docs' metadata and relationships.

The Obsidian vault remains the source of truth, so HQ stores no runbook bodies
and no secrets, only enough to point an assistant at the right place.
"""

from typing import override

from django.db import models
from django.db.models import Q

from hq.platform.application.display_words import QuotedCharField
from hq.platform.application.labels import dotted
from hq.platform.application.references import Kind
from hq.platform.application.tone import Tone, TonedChoices
from hq.platform.application.ui import ended
from hq.platform.core.models import TimestampedModel
from hq.platform.core.rules import Rule
lazy from hq.platform.application.entity_links import page_url


class DocStatus(TonedChoices):
    DRAFT = "draft", Tone.NEUTRAL, "Draft"
    ACTIVE = "active", Tone.GOOD, "Active"
    # Still read, and due its replacement or its archive.
    DEPRECATED = "deprecated", Tone.ATTENTION, "Deprecated"
    ARCHIVED = "archived", Tone.NEUTRAL, "Archived"


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

    # A classification, not a standing, so it takes no tone: a page draws
    # it as a chip in its own words.
    class Sensitivity(models.TextChoices):
        PUBLIC = "public", "Public"
        INTERNAL = "internal", "Internal"
        SENSITIVE = "sensitive", "Sensitive"
        RESTRICTED = "restricted", "Restricted"

    doc_id = models.SlugField("doc ID", max_length=80, unique=True)
    title = QuotedCharField(max_length=200)
    doc_type = models.CharField(max_length=32, choices=DocType.choices, default=DocType.RUNBOOK)
    system_service = models.CharField(max_length=120, blank=True)
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.OTHER,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    sensitivity = models.CharField(max_length=20, choices=Sensitivity.choices, default=Sensitivity.INTERNAL)

    obsidian_path = models.CharField(max_length=400, blank=True)
    github_path = models.CharField(max_length=400, blank=True)
    external_url = models.URLField("external URL", blank=True)

    # The vault stamps it on every edit of the doc, so it is when the doc last changed.
    last_reviewed = models.DateField("last changed", null=True, blank=True)
    published_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    related_projects = models.ManyToManyField("projects.Project", blank=True, related_name="documentation_records")
    related_assets = models.ManyToManyField("assets.Asset", blank=True, related_name="documentation_records")
    related_expenses = models.ManyToManyField("expenses.Expense", blank=True, related_name="documentation_records")

    class Meta:
        ordering = ("-last_reviewed",)
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
        return dotted(self.doc_id, self.title)

    def get_absolute_url(self) -> str:
        return page_url(Kind.DOCUMENT, self.doc_id)

    @property
    def state(self) -> DocStatus:
        """Its status as the member its pill is drawn from, in its words and tone."""

        return DocStatus(self.status)

    @property
    def is_safe_for_ai_export(self) -> bool:
        return self.sensitivity in _SAFE_SENSITIVITIES


# Which sensitivities an AI-facing surface may name, beside the choices it is
# made of, and read only as AI_SAFE or is_safe_for_ai_export.
_SAFE_SENSITIVITIES = (DocumentationRecord.Sensitivity.PUBLIC, DocumentationRecord.Sensitivity.INTERNAL)
# The documentation records an AI-facing surface may name, as a query filter.
AI_SAFE = Q(sensitivity__in=_SAFE_SENSITIVITIES)
