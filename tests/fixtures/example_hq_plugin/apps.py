from django.apps import AppConfig


class ExamplePluginConfig(AppConfig):
    name = 'tests.fixtures.example_hq_plugin'
    label = 'example_hq_plugin'
    verbose_name = "HQ Plugin Contract Example"
