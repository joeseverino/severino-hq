"""The forecast is kept as an upstream reading; the next refresh fills it."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0061_each_playbook_is_adopted_on_its_own"),
    ]

    operations = [
        migrations.DeleteModel(name="WeatherObservation"),
    ]
