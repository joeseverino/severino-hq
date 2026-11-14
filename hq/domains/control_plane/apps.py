from typing import override

from django.apps import AppConfig

READ_REQUEST = "Read request"
DASHBOARD = "Dashboard"


class ControlPlaneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.domains.control_plane"
    label = "control_plane"
    verbose_name = "Infrastructure Control Plane"

    @override
    def ready(self):
        from hq.platform.application.approvals import AUDIT_LABEL as APPROVAL_AUDIT_LABEL
        from hq.platform.core.audit import register_audit

        from .models import (
            AdoptedCatalog,
            ApprovalRequest,
            CapabilityRule,
            DashboardConfiguration,
            DashboardMachine,
            DashboardRefreshRequest,
            ManagedResource,
            NotManaged,
            OperationRequest,
            ReadRequest,
            SealedSecret,
            SourceProposal,
        )

        register_audit(
            ManagedResource,
            "Managed resource",
            # A sweep stamps this on every declaration it confirms. That is HQ
            # reporting that it looked, not the world reporting that it moved.
            observation=("last_observed_at",),
            connection=_resource_connection,
            redact_for=_withheld_fields,
            subject=lambda resource: resource.key,
        )
        register_audit(
            OperationRequest,
            "Infrastructure operation",
            connection=lambda operation: _resource_connection(operation.resource),
            subject=lambda operation: operation.resource.key,
        )
        # An operator's choice that a record stays unmanaged, and its reversal.
        register_audit(NotManaged, "Not managed")
        # Who asked for a held change, who agreed to it and when.
        register_audit(ApprovalRequest, APPROVAL_AUDIT_LABEL, subject=lambda held: held.resource_key)
        # Who asked for a connection to be read now, and when.
        register_audit(ReadRequest, READ_REQUEST, connection=lambda request: request.connection_ref)
        # Who asked for a dashboard panel to be read now, filed with the other
        # asks for a reading, so the log's one toggle shows or hides them all.
        register_audit(DashboardRefreshRequest, READ_REQUEST, observation=("completed_at",), required=True)
        # What the dashboard shows, and on which machines, under one label.
        register_audit(DashboardConfiguration, DASHBOARD, required=True)
        register_audit(DashboardMachine, DASHBOARD, subject=lambda placement: placement.machine.key, required=True)
        # What a Refresh found in the automation source, under the source's key.
        register_audit(SourceProposal, "Playbook scan result", subject=lambda proposal: proposal.operation.resource.key)
        # Which commit of it became the catalog HQ runs from, and who adopted it.
        register_audit(AdoptedCatalog, "Adopted playbooks", subject=_source_key)
        # An operator's rule for what one agent may run.
        register_audit(CapabilityRule, "Capability policy", required=True)
        # A secret handed to one resource: that it was set, never what it holds.
        register_audit(
            SealedSecret,
            "Sealed secret",
            redact=("sealed",),
            subject=lambda secret: secret.resource.key,
            required=True,
        )


def _source_key(_catalog) -> str:
    # Imported once an adoption is audited, so the app's start loads no validation library.
    from hq.platform.application.automation_sources import SOURCE_KEY

    return SOURCE_KEY


def _withheld_fields(resource) -> tuple[str, ...]:
    """The spec of a withheld kind is logged as changed, never with its values."""

    from .providers import spec_withheld

    return ("spec",) if spec_withheld(resource.kind) else ()


def _resource_connection(resource) -> str:
    """The connection a resource's work goes through, from its spec."""

    spec = resource.spec if isinstance(resource.spec, dict) else {}
    return str(spec.get("connection_ref", "") or "")
