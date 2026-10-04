from django.db import migrations, models


def secret_adjacent_to_restricted(apps, schema_editor):
    DocumentationRecord = apps.get_model("docs_index", "DocumentationRecord")
    DocumentationRecord.objects.filter(sensitivity="secret_adjacent").update(
        sensitivity="restricted"
    )


def restricted_to_secret_adjacent(apps, schema_editor):
    DocumentationRecord = apps.get_model("docs_index", "DocumentationRecord")
    DocumentationRecord.objects.filter(sensitivity="restricted").update(
        sensitivity="secret_adjacent"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("docs_index", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(
            secret_adjacent_to_restricted,
            reverse_code=restricted_to_secret_adjacent,
        ),
        migrations.AlterField(
            model_name="documentationrecord",
            name="sensitivity",
            field=models.CharField(
                choices=[
                    ("public", "Public"),
                    ("internal", "Internal"),
                    ("sensitive", "Sensitive"),
                    ("restricted", "Restricted"),
                ],
                default="internal",
                help_text=(
                    "Sensitivity label. Public/internal docs can be referenced "
                    "by the future MCP; sensitive/restricted should not be."
                ),
                max_length=20,
            ),
        ),
    ]
