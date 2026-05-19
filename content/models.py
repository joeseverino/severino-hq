"""Content pipeline."""

from __future__ import annotations

from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from core.models import TimestampedModel


class ContentItem(TimestampedModel):
    class Type(models.TextChoices):
        ARTICLE = "article", "Article"
        VIDEO = "video", "Video"
        LAB_WRITEUP = "lab_writeup", "Lab writeup"
        GUIDE = "guide", "Guide"
        REVIEW = "review", "Review"
        PORTFOLIO_PAGE = "portfolio_page", "Portfolio page"
        SERVICE_PAGE = "service_page", "Service page"
        CASE_STUDY = "case_study", "Case study"
        PAGE = "page", "Page"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    content_type = models.CharField(
        max_length=24, choices=Type.choices, default=Type.ARTICLE
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    topic = models.CharField(max_length=160, blank=True)
    tags = models.CharField(
        max_length=300,
        blank=True,
        help_text="Comma-separated tags.",
    )
    published_url = models.URLField(blank=True)
    wordpress_post_id = models.PositiveIntegerField(null=True, blank=True)
    wordpress_slug = models.SlugField(max_length=200, blank=True)
    published_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    related_projects = models.ManyToManyField(
        "projects.Project", blank=True, related_name="content_items"
    )
    related_assets = models.ManyToManyField(
        "assets.Asset", blank=True, related_name="content_items"
    )
    related_expenses = models.ManyToManyField(
        "expenses.Expense", blank=True, related_name="content_items"
    )
    related_documentation = models.ManyToManyField(
        "docs_index.DocumentationRecord",
        blank=True,
        related_name="content_items",
    )

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("content_type",)),
            models.Index(fields=("published_at",)),
        ]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title) or "content-item"
            slug = base
            n = 2
            while (
                ContentItem.objects.filter(slug=slug).exclude(pk=self.pk).exists()
            ):
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("content:detail", args=[self.slug])

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def is_published(self) -> bool:
        return self.status == self.Status.PUBLISHED
