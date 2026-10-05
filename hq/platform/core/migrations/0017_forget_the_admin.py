"""Remove what ``django.contrib.admin`` left in the database.

The app is not installed, so nothing owns its log table, and a table with a
foreign key to the user and nothing to cascade through refuses the deletion of
any user it names. Its migration records go with it, so the database does not
claim a schema it no longer holds.
"""

from django.db import migrations

FORGET = (
    "DROP TABLE IF EXISTS django_admin_log",
    "DELETE FROM django_migrations WHERE app = 'admin'",
    "DELETE FROM auth_group_permissions WHERE permission_id IN ("
    "SELECT auth_permission.id FROM auth_permission JOIN django_content_type "
    "ON django_content_type.id = auth_permission.content_type_id "
    "WHERE django_content_type.app_label = 'admin')",
    "DELETE FROM auth_user_user_permissions WHERE permission_id IN ("
    "SELECT auth_permission.id FROM auth_permission JOIN django_content_type "
    "ON django_content_type.id = auth_permission.content_type_id "
    "WHERE django_content_type.app_label = 'admin')",
    "DELETE FROM auth_permission WHERE content_type_id IN ("
    "SELECT id FROM django_content_type WHERE app_label = 'admin')",
    "DELETE FROM django_content_type WHERE app_label = 'admin'",
)


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0016_rules"),
    ]

    operations = [migrations.RunSQL(sql=FORGET, reverse_sql=migrations.RunSQL.noop)]
