"""
Documentation Index.

Severino HQ does NOT store full runbook bodies. The Obsidian vault remains the
source of truth. This model stores metadata and relationships only, so the
future severino-knowledge-router MCP can point an assistant at the right place.

No secrets should be stored here.
"""

from __future__ import annotations

from django.db import models
from django.urls import reverse

from core.models import TimestampedModel


class DocumentationRecord(TimestampedModel):
    class DocType(models.TextChoices):
        RUNBOOK = "runbook", "Runbook"
        ARCHITECTURE_NOTE = "architecture_note", "Architecture note"
        DEPLOYMENT_GUIDE = "deployment_guide", "Deployment guide"
        TROUBLESHOOTING_GUIDE = "troubleshooting_guide", "Troubleshooting guide"
        RECOVERY_PROCEDURE = "recovery_procedure", "Recovery procedure"
        PUBLIC_ARTICLE_DRAFT = "public_article_draft", "Public article draft"
        DECISION_RECORD = "decision_record", "Decision record"
        TASK = "task", "Task"

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

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        DEPRECATED = "deprecated", "Deprecated"
        ARCHIVED = "archived", "Archived"

    class TaskStatus(models.TextChoices):
        # A task doc carries its own lifecycle (the importer writes these into the
        # same status field). Mirrors the schema's task_statuses — guarded in tests.
        OPEN = "open", "Open"
        ACTIVE = "active", "Active"
        PARKED = "parked", "Parked"
        DONE = "done", "Done"
        WONTFIX = "wontfix", "Won't fix"

    # The status field holds a standard doc status OR a task lifecycle status.
    # Union the two, deduped by value (both declare "active"), so admin/forms
    # accept and label every value the importer writes instead of rejecting a
    # task's "open"/"done". Guarded against the schema union in tests.
    STATUS_CHOICES = list(dict.fromkeys([*Status.choices, *TaskStatus.choices]))

    class Sensitivity(models.TextChoices):
        PUBLIC = "public", "Public"
        INTERNAL = "internal", "Internal"
        SENSITIVE = "sensitive", "Sensitive"
        RESTRICTED = "restricted", "Restricted"

    doc_id = models.SlugField(
        max_length=80,
        unique=True,
        help_text=(
            "Stable identifier, e.g. 'rb-adguard-001'. Used by the future "
            "knowledge-router MCP and JSON exports."
        ),
    )
    title = models.CharField(max_length=200)
    doc_type = models.CharField(
        max_length=32, choices=DocType.choices, default=DocType.RUNBOOK
    )
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
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=Status.DRAFT
    )
    sensitivity = models.CharField(
        max_length=20,
        choices=Sensitivity.choices,
        default=Sensitivity.INTERNAL,
        help_text=(
            "Sensitivity label. Public/internal docs can be referenced by the "
            "future MCP; sensitive/restricted should not be."
        ),
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

    related_projects = models.ManyToManyField(
        "projects.Project", blank=True, related_name="documentation_records"
    )
    related_assets = models.ManyToManyField(
        "assets.Asset", blank=True, related_name="documentation_records"
    )
    related_expenses = models.ManyToManyField(
        "expenses.Expense", blank=True, related_name="documentation_records"
    )

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("doc_type",)),
            models.Index(fields=("environment",)),
            models.Index(fields=("sensitivity",)),
            models.Index(fields=("last_reviewed",)),
        ]
        verbose_name = "Documentation record"

    def __str__(self) -> str:
        return f"{self.doc_id} — {self.title}"

    def get_absolute_url(self) -> str:
        return reverse("docs_index:detail", args=[self.doc_id])

    @property
    def is_safe_for_ai_export(self) -> bool:
        return self.sensitivity in {
            self.Sensitivity.PUBLIC,
            self.Sensitivity.INTERNAL,
        }
