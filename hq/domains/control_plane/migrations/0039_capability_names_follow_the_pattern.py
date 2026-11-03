from django.db import migrations

from hq.platform.application.capability_renames import RenameCapabilities


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0038_approval_key_names_its_asker"),
    ]

    operations = [
        RenameCapabilities(
            {
                "action_items.restore": "action_item.restore",
                "action_items.set_aside": "action_item.set_aside",
                "agents.access.set": "agent.access.set",
                "agents.block.set": "agent.block.set",
                "agents.device.set": "agent.device.set",
                "agents.policy.set": "agent.policy.set",
                "contact.submissions.list": "contact.submission.list",
                "dashboard.refresh.request": "dashboard.panels.refresh",
                "dashboard.refresh.stale": "dashboard.panels.refresh_due",
                "infrastructure.reconcile": "infrastructure.resource.reconcile",
                "infrastructure.restart": "infrastructure.resource.restart",
                "infrastructure.start": "infrastructure.resource.start",
                "infrastructure.stop": "infrastructure.resource.stop",
                "lookup.address": "lookup.address.read",
                "lookup.name": "lookup.name.read",
                "project.dependency_updates": "project.dependency_updates.set",
                "sessions.sign_out": "session.sign_out",
            }
        ),
    ]
