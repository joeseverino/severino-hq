from django.apps import AppConfig


class FixtureDomainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.platform.application.tests.fixture_domain"
    label = "fixture_domain"
