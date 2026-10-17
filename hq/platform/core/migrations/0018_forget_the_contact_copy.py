"""Remove the inbox copy HQ kept before the controller read the contact form.

The stored reading under ``contacts.unread`` held the newest submitters' names
and the unread count. Nothing writes or reads it now: the controller's
``cloudflare.contact_submission`` reading replaced it, so the row would only be
a stale copy of people's names.
"""

from django.db import migrations


def forget(apps, schema_editor):
    apps.get_model("core", "UpstreamReading").objects.filter(key="contacts.unread").delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0017_forget_the_admin")]

    operations = [migrations.RunPython(forget, migrations.RunPython.noop)]
