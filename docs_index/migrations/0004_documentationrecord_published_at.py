from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("docs_index", "0003_rename_secret_adjacent_to_restricted"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentationrecord",
            name="published_at",
            field=models.DateField(
                blank=True,
                null=True,
                help_text=(
                    "Publication date for public articles. "
                    "Empty for operational docs."
                ),
            ),
        ),
    ]
