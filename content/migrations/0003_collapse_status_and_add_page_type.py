"""Collapse ContentItem.Status to draft/published/archived and add Page type.

Old states `idea`, `researching`, `drafting`, `editing` all fold into `draft`.
The publishing-pipeline kanban was aspirational for solo authoring; the simpler
model mirrors the vault's `published` boolean directly.
"""

from django.db import migrations, models


COLLAPSE_MAP = {
    "idea": "draft",
    "researching": "draft",
    "drafting": "draft",
    "editing": "draft",
}


def collapse_statuses(apps, schema_editor):
    ContentItem = apps.get_model("content", "ContentItem")
    for old, new in COLLAPSE_MAP.items():
        ContentItem.objects.filter(status=old).update(status=new)


def reverse_noop(apps, schema_editor):
    # No reverse mapping — once collapsed, the prior fidelity is gone.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(collapse_statuses, reverse_noop),
        migrations.AlterField(
            model_name="contentitem",
            name="content_type",
            field=models.CharField(
                choices=[
                    ("article", "Article"),
                    ("video", "Video"),
                    ("lab_writeup", "Lab writeup"),
                    ("guide", "Guide"),
                    ("review", "Review"),
                    ("portfolio_page", "Portfolio page"),
                    ("service_page", "Service page"),
                    ("case_study", "Case study"),
                    ("page", "Page"),
                ],
                default="article",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="contentitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("published", "Published"),
                    ("archived", "Archived"),
                ],
                default="draft",
                max_length=20,
            ),
        ),
    ]
