"""Who acted becomes a column over the audit rows HQ already holds."""

from .migration_steps import MigrationStepTestCase

BEFORE = ("core", "0028_idempotency_record")
AFTER = ("core", "0029_audit_actor_and_interface")


class AuditActorMigrationTests(MigrationStepTestCase):
    def test_a_signed_in_users_row_names_them_as_its_actor(self):
        apps = self.migrate(BEFORE)
        user = apps.get_model("auth", "User").objects.create(username="example-operator")
        audit = apps.get_model("core", "AuditLog")
        by_agent = audit.objects.create(
            action="updated", user=user, metadata={"actor": "example-agent", "interface": "mcp"}
        ).pk
        by_hand = audit.objects.create(action="updated", user=user, metadata={"changes": {}}).pk
        by_nobody = audit.objects.create(action="observed", metadata={}).pk

        apps = self.migrate(AFTER)

        found = dict(apps.get_model("core", "AuditLog").objects.values_list("pk", "actor"))
        self.assertEqual((found[by_agent], found[by_hand], found[by_nobody]), ("example-agent", "example-operator", ""))
