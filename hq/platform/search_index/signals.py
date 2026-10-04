from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .registry import BY_MODEL
from .services import index_instance, remove_instance


@receiver(post_save, weak=False)
def update_search_projection(sender, instance, raw=False, **kwargs):
    definition = BY_MODEL.get(sender)
    if definition is not None and not raw:
        index_instance(definition, instance)


@receiver(post_delete, weak=False)
def delete_search_projection(sender, instance, **kwargs):
    definition = BY_MODEL.get(sender)
    if definition is not None:
        remove_instance(definition, instance)
