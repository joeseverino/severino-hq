from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="operation_id",
            field=models.CharField(blank=True, db_index=True, max_length=128),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("updated", "Updated"),
                    ("deleted", "Deleted"),
                    ("login", "Login"),
                    ("logout", "Logout"),
                    ("login_failed", "Login failed"),
                    ("uploaded", "Uploaded"),
                    ("exported", "Exported"),
                    ("imported", "Imported"),
                    ("failed", "Failed"),
                    ("settings_changed", "Settings changed"),
                    ("viewed", "Viewed"),
                ],
                max_length=32,
            ),
        ),
    ]
