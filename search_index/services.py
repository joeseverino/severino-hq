"""Maintain and rebuild the relational search projection."""

from __future__ import annotations

from django.db import transaction

from .models import SearchDocument
from application.search_contracts import SearchDefinition

from .registry import DEFINITIONS


def index_instance(definition: SearchDefinition, instance) -> None:
    SearchDocument.objects.update_or_create(
        scope=definition.scope,
        object_id=definition.object_id(instance),
        defaults={"body": definition.body(instance)},
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
