from typing import override

from django.db import models
from django.urls import reverse


class Note(models.Model):
    """One note, and the day it was written."""

    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=120)
    written_on = models.DateField()

    class Meta:
        ordering = ("-written_on", "slug")

    @override
    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("example_extension:note", args=[self.slug])
