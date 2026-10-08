from django.apps import AppConfig


class ExpensesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.domains.expenses'
    label = 'expenses'
    verbose_name = "Expenses"

    def ready(self):
        from hq.platform.core.audit import register_audit

        from .models import Expense

        register_audit(Expense, "Expense")
