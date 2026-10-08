"""Maintain and rebuild the relational search projection."""

from django.db import transaction

from .models import SearchDocument
from hq.platform.application.search_contracts import SearchDefinition

from .registry import DEFINITIONS


def index_instance(definition: SearchDefinition, instance) -> None:
    """Store the instance's search body, writing only when it differs.

    A save that leaves the body as it stands writes nothing: no row, no
    full-text entry and no revision. A body that is new or changed is one
    statement, which inserts the document or replaces its body.
    """

    key = {"scope": definition.scope, "object_id": definition.object_id(instance)}
    body = definition.body(instance)
    stored = SearchDocument.objects.filter(**key).values_list("body", flat=True).first()
    if stored == body:
        return
    SearchDocument.objects.bulk_create(
        [SearchDocument(**key, body=body)],
        update_conflicts=True,
        unique_fields=("scope", "object_id"),
        update_fields=("body", "updated_at"),
    )


def remove_instance(definition: SearchDefinition, instance) -> None:
    SearchDocument.objects.filter(
        scope=definition.scope,
        object_id=definition.object_id(instance),
    ).delete()


@transaction.atomic
def rebuild_search_index() -> dict[str, int]:
    SearchDocument.objects.all().delete()
    counts = {}
    for definition in DEFINITIONS:
        documents = [
            SearchDocument(
                scope=definition.scope,
                object_id=definition.object_id(instance),
                body=definition.body(instance),
            )
            for instance in definition.model.objects.all().iterator(chunk_size=500)
        ]
        SearchDocument.objects.bulk_create(documents, batch_size=500)
        counts[definition.scope] = len(documents)
    return counts
