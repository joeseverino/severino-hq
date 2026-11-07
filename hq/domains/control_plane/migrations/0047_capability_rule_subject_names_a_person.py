"""A rule's subject holds a client id and its person's sign-in key."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0046_approval_records_the_person")]

    operations = [
        migrations.AlterField(
            model_name="capabilityrule",
            name="subject",
            field=models.CharField(max_length=255),
        ),
    ]
