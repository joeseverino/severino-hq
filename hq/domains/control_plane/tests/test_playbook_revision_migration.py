"""A catalog adopted as one commit becomes one whose every procedure pins
that commit and the files of it no procedure runs, and loads as one."""

from hq.platform.core.tests.migration_steps import MigrationStepTestCase

BEFORE = ("control_plane", "0060_a_person_rejects_a_scan")
AFTER = ("control_plane", "0061_each_playbook_is_adopted_on_its_own")
REVISION = "1" * 40
# A procedure as a catalog of one commit stored it.
STORED = {
    "id": "example.check",
    "target_kinds": ["machine"],
    "hosts": [],
    "privilege": None,
    "effect": "read",
    "timeout_seconds": 60,
    "recovery": ["Look at the machine."],
    "retry": "safe",
    "fixes": [],
    "playbook": "playbooks/check.yml",
    "title": "The check procedure",
    "preconditions": [],
    "verification": [],
    "inputs": {},
    "files": {"playbooks/check.yml": "a" * 40},
}


class PlaybookRevisionMigrationTests(MigrationStepTestCase):
    def setUp(self):
        catalogs = self.migrate(BEFORE).get_model("control_plane", "AdoptedCatalog").objects
        catalogs.create(
            branch="main",
            revision=REVISION,
            procedures=[STORED],
            files=[{"path": "playbooks/check.yml", "object": "a" * 40}, {"path": "README.md", "object": "c" * 40}],
            digest="0" * 64,
        )

    def test_each_procedure_pins_the_commit_and_its_shared_files_and_the_catalog_loads(self):
        from hq.platform.application.automation_adoption import catch_up, manifests, procedures

        self.migrate(AFTER)

        digest = catch_up()
        (procedure,) = procedures(digest)
        self.assertEqual(procedure.revision, REVISION)
        self.assertEqual(procedure.files, {"playbooks/check.yml": "a" * 40})
        self.assertEqual(procedure.shared, {"README.md": "c" * 40})
        (manifest,) = manifests(digest)
        self.assertEqual(manifest.revision, REVISION)
