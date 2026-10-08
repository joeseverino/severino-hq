from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.domains.contacts"
    label = "contacts"
    verbose_name = "Messages"
