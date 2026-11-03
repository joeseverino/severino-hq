from django.apps import AppConfig


# Not audited by its model: a job's one row in the log is the runner's, written as it ends.
class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.domains.jobs"
    label = "jobs"
    verbose_name = "Background jobs"
