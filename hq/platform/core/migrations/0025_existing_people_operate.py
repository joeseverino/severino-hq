"""Every account that exists operates HQ, as each did before roles, until its next sign-in resolves its role."""

from django.conf import settings
from django.db import migrations


def operate(apps, schema_editor):
    users = apps.get_model(settings.AUTH_USER_MODEL)
    roles = apps.get_model("core", "PersonRole")
    held = set(roles.objects.values_list("user_id", flat=True))
    roles.objects.bulk_create(
        roles(user_id=pk, role="operator") for pk in users.objects.values_list("pk", flat=True) if pk not in held
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0024_person_role")]

    operations = [migrations.RunPython(operate, migrations.RunPython.noop)]
