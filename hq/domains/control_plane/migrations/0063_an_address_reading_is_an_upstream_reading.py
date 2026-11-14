"""What the registries say about an address is kept as an upstream reading; the next lookup fills it."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0062_weather_is_an_upstream_reading"),
    ]

    operations = [
        migrations.DeleteModel(name="AddressReading"),
    ]
