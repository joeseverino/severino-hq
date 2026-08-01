from django.db import models


class SearchDocument(models.Model):
    """Portable search projection; FTS5 is an index over this stable contract."""

    scope = models.CharField(max_length=40)
    object_id = models.CharField(max_length=200)
    body = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("scope", "object_id"),
                name="search_document_scope_object_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("scope", "object_id"),
                name="search_idx_scope_obj",
            )
        ]
