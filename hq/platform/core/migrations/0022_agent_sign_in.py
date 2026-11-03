from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0021_own_device")]

    operations = [
        migrations.AddField(
            model_name="agentidentity",
            name="signs_in",
            field=models.CharField(
                blank=True,
                choices=[("machine", "As itself"), ("person", "Through a person's sign-in")],
                default="",
                max_length=16,
            ),
        ),
    ]
