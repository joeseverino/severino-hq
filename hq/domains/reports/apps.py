from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.domains.reports'
    label = 'reports'
    verbose_name = "Reports & Exports"
