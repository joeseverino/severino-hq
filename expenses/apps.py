from django.apps import AppConfig


class ExpensesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "expenses"
    verbose_name = "Expenses"

    def ready(self):
        from core.audit import register_audit
        from .models import Expense

        register_audit(Expense, "Expense")
