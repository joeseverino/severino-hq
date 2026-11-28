"""Maintain and rebuild the relational search projection."""

from django.db import transaction

from hq.platform.application.resources import resource_search_definitions
from hq.platform.application.search_contracts import SearchDefinition

from .models import SearchDocument


def index_instance(definition: SearchDefinition, instance) -> None:
    """Store the instance's search columns, writing only when they differ.

    A save that leaves them as they stand writes nothing: no row, no
    full-text entry and no revision. Columns that are new or changed are one
    statement, which inserts the document or replaces them.
    """

    key = {"scope": definition.scope, "object_id": definition.object_id(instance)}
    columns = definition.columns(instance)
    stored = SearchDocument.objects.filter(**key).values(*columns).first()
    if stored == columns:
        return
    SearchDocument.objects.bulk_create(
        [SearchDocument(**key, **columns)],
        update_conflicts=True,
        unique_fields=("scope", "object_id"),
        update_fields=(*columns, "updated_at"),
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
    for definition in resource_search_definitions():
        documents = [
            SearchDocument(
                scope=definition.scope,
                object_id=definition.object_id(instance),
                **definition.columns(instance),
            )
            for instance in definition.model.objects.all().iterator(chunk_size=500)
        ]
        SearchDocument.objects.bulk_create(documents, batch_size=500)
        counts[definition.scope] = len(documents)
    return counts
