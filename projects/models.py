"""Projects / labs."""

from __future__ import annotations

from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from core.models import TimestampedModel


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


class Project(TimestampedModel):
    class Status(models.TextChoices):
        IDEA = "idea", "Idea"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    category = models.CharField(
        max_length=40,
        choices=PROJECT_CATEGORY_CHOICES,
        default="other",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.IDEA
    )
    description = models.TextField(blank=True)
    technologies_used = models.CharField(
        max_length=300,
        blank=True,
        help_text="Comma-separated list of technologies.",
    )
    repository_url = models.URLField(blank=True)
    last_push_at = models.DateTimeField(null=True, blank=True)
    public_url = models.URLField(blank=True)
    deployment_notes = models.TextField(blank=True)
    security_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("category",)),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "project"
            slug = base
            n = 2
            while Project.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("projects:detail", args=[self.slug])

    @property
    def tech_list(self) -> list[str]:
        return [t.strip() for t in self.technologies_used.split(",") if t.strip()]
