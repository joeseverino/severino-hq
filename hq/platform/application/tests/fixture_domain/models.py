from django.db import models


class Widget(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=80)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name
