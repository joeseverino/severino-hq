"""A rule set for a client by its id alone, before a client as a person was
its own agent, is carried to each person's row of that client, so the Agents
page is found as it was left. A rule for a client with no person row stays.
"""

from django.db import migrations

SEPARATOR = "@"


def carry_rules_to_each_person(apps, schema_editor):
    AgentIdentity = apps.get_model("core", "AgentIdentity")
    CapabilityRule = apps.get_model("control_plane", "CapabilityRule")
    keys = {}
    for client_id, person_key in AgentIdentity.objects.exclude(person_key="").values_list("client_id", "person_key"):
        keys.setdefault(client_id, []).append(person_key)
    for rule in CapabilityRule.objects.filter(subject__in=list(keys)):
        for person_key in keys[rule.subject]:
            CapabilityRule.objects.get_or_create(
                subject=f"{rule.subject}{SEPARATOR}{person_key}",
                capability=rule.capability,
                defaults={"rule": rule.rule, "changed_by": rule.changed_by, "changed_at": rule.changed_at},
            )
        # Kept only where the client also calls as itself.
        if not AgentIdentity.objects.filter(client_id=rule.subject, person_key="").exists():
            rule.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0048_one_pending_approval_per_asker_and_person"),
        ("core", "0034_agent_identity_one_per_client_and_person"),
    ]

    operations = [migrations.RunPython(carry_rules_to_each_person, migrations.RunPython.noop)]
