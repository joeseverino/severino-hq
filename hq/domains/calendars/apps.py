from django.apps import AppConfig


class CalendarsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.domains.calendars'
    label = 'calendars'
    verbose_name = "Calendar"

    def ready(self):
        from hq.platform.core.audit import register_audit
        from .models import Entry

        register_audit(Entry, "Calendar entry")
