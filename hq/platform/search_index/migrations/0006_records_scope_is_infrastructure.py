"""The managed records' documents move to their resource's new name, which a search scope is."""

from django.db import migrations

OLD = "infrastructure.resources"
NEW = "infrastructure"


def rescope(old, new):
    def move(apps, schema_editor):
        apps.get_model("search_index", "SearchDocument").objects.filter(scope=old).update(scope=new)

    return move


class Migration(migrations.Migration):
    dependencies = [
        ("search_index", "0005_drop_duplicate_index"),
    ]

    operations = [migrations.RunPython(rescope(OLD, NEW), rescope(NEW, OLD))]
