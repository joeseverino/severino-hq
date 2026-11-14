from typing import override

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.platform.core"
    label = "core"

    @override
    def ready(self):
        from django.db.models.signals import post_migrate

        from . import checks, signals  # noqa: F401 - imported to register checks and receivers
        from .outbound import install as hold_requests
        from .print_gate import install as gate_prints

        # From here on a request cannot wait on anything outside the process.
        hold_requests()
        # And a page prints only text and numbers.
        gate_prints()
        from .revisions import after_migrate

        # Sent after the whole migrate, so every app's tables exist.
        post_migrate.connect(after_migrate, sender=self, dispatch_uid="hq.revisions")
        _audit_people_and_agents()


def _audit_people_and_agents() -> None:
    """Each change to a person's settings or an agent's standing is its row's audit event."""

    from .audit import register_audit
    from .models import (
        AgentAccess,
        AgentDevice,
        AgentIdentity,
        Appearance,
        Avatar,
        LinkedAccount,
        OwnDevice,
        PersonRole,
    )

    # Each commits with its row or not at all.
    register_audit(Appearance, "Appearance", required=True)
    # Confirming a role at each sign-in is HQ looking; a new role is the event.
    register_audit(PersonRole, "Role", observation=("resolved_at",), required=True)
    register_audit(LinkedAccount, "Linked account", required=True)
    # A new picture is the event, which its digest says; fetching the same one again is not.
    register_audit(
        Avatar,
        "Avatar",
        redact=("image",),
        observation=("image", "fetched_at", "source", "content_type"),
        required=True,
    )
    register_audit(OwnDevice, "Own device", required=True)
    register_audit(AgentAccess, "Agent access", required=True)
    # What a call shows of its agent and device as it arrives is HQ looking;
    # a grant, a person, an approval or a block is the event.
    register_audit(AgentIdentity, "Agent identity", observation=("last_seen", "interfaces", "signs_in"), required=True)
    register_audit(AgentDevice, "Agent device", observation=("last_seen", "name", "os", "owner"), required=True)
