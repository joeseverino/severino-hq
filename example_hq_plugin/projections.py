from django.urls import reverse


def dashboard_cards():
    return (
        {
            "id": "example-notes",
            "label": "Example records",
            "value": "0",
            "url": reverse("example_plugin:index"),
        },
    )


def ready():
    return True
