"""A reading's key has room for its kind and a machine's whole key."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_dated_rule_messages"),
    ]

    operations = [
        migrations.AlterField(
            model_name="upstreamreading",
            name="key",
            field=models.CharField(max_length=255, primary_key=True, serialize=False),
        ),
    ]
