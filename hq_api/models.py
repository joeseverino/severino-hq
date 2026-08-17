"""Transport state that makes machine retries safe across process restarts."""

from django.db import models


class IdempotencyRecord(models.Model):
    actor = models.CharField(max_length=255)
    actor_sha256 = models.CharField(max_length=64)
    key = models.CharField(max_length=128)
    request_sha256 = models.CharField(max_length=64)
    response = models.JSONField(null=True)
    status_code = models.PositiveSmallIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("actor_sha256", "key"),
                name="unique_api_idempotency_key_per_actor",
            )
        ]
        ordering = ("-created_at",)
