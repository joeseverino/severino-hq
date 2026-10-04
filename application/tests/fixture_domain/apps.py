from django.apps import AppConfig


class FixtureDomainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "application.tests.fixture_domain"
    label = "fixture_domain"
