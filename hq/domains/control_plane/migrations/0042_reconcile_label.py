from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0041_refused_report"),
    ]

    operations = [
        migrations.AlterField(
            model_name="operationrequest",
            name="action",
            field=models.CharField(
                choices=[
                    ("reconcile", "Apply HQ's config"),
                    ("renew", "Renew certificate"),
                    ("delete", "Remove"),
                    ("restart", "Restart"),
                    ("start", "Start"),
                    ("stop", "Stop"),
                    ("approve-routes", "Approve routes"),
                ],
                max_length=20,
            ),
        ),
    ]
