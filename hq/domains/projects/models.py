"""Projects / labs."""

from typing import override

from django.db import models
from django.urls import reverse

from hq.platform.application.lists import split_list
from hq.platform.application.references import Referable
from hq.platform.application.tone import Tone, TonedChoices
from hq.platform.core.models import TimestampedModel, free_slug

PROJECT_CATEGORY_CHOICES = [
    ("wordpress_security", "WordPress security"),
    ("cloudflare", "Cloudflare"),
    ("dns_email_security", "DNS / email security"),
    ("homelab", "Homelab"),
    ("vps", "VPS"),
    ("tailscale", "Tailscale"),
    ("adguard", "AdGuard"),
    ("networking", "UniFi / networking"),
    ("tls_certificates", "TLS / certificates"),
    ("automation", "Automation / scripts"),
    ("cybersecurity_labs", "Cybersecurity labs"),
    ("other", "Other"),
]


class ProjectQuerySet(models.QuerySet["Project"]):
    def _without(self, relation: str) -> models.Exists:
        through = getattr(self.model, relation).through
        return ~models.Exists(through.objects.filter(project=models.OuterRef("pk")))

    def no_content(self) -> ProjectQuerySet:
        """Projects no writeup or page names."""

        return self.filter(self._without("content_items"))

    def no_docs(self) -> ProjectQuerySet:
        """Projects no document names."""

        return self.filter(self._without("documentation_records"))

    def updates_on(self) -> ProjectQuerySet:
        """Projects HQ's dependency update run covers."""

        return self.filter(dependency_updates=True)

    def needs_output(self) -> ProjectQuerySet:
        """Active projects still missing a writeup or a document."""

        return self.filter(
            models.Q(self._without("content_items")) | models.Q(self._without("documentation_records")),
            status=Project.Status.ACTIVE,
        )


class Project(TimestampedModel):
    class Status(TonedChoices):
        IDEA = "idea", Tone.NEUTRAL, "Idea"
        ACTIVE = "active", Tone.GOOD, "Active"
        PAUSED = "paused", Tone.ATTENTION, "Paused"
        COMPLETED = "completed", Tone.GOOD, "Completed"
        PUBLISHED = "published", Tone.GOOD, "Published"
        ARCHIVED = "archived", Tone.NEUTRAL, "Archived"

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    category = models.CharField(
        max_length=40,
        choices=PROJECT_CATEGORY_CHOICES,
        default="other",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IDEA)
    description = models.TextField(blank=True)
    technologies_used = models.CharField(
        max_length=300,
        blank=True,
        help_text="Comma-separated list of technologies.",
    )
    repository_url = models.URLField("repository URL", blank=True)
    last_push_at = models.DateTimeField(null=True, blank=True)
    # Whether HQ's dependency update run covers the repository. Set only by the
    # `project.dependency_updates.set` capability, which holds its precondition.
    dependency_updates = models.BooleanField(default=False)
    public_url = models.URLField("public URL", blank=True)
    deployment_notes = models.TextField(blank=True)
    security_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    referable = Referable(kind="project", key="slug", shows=("name",))

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("category",)),
        ]

    @override
    def __str__(self) -> str:
        return self.name

    @override
    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = free_slug(self, self.name, "project")
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("projects:detail", args=[self.slug])

    @property
    def state(self) -> Project.Status:
        """Its status as the member its pill is drawn from, in its words and tone."""

        return self.Status(self.status)

    @property
    def tech_list(self) -> list[str]:
        return split_list(self.technologies_used)
