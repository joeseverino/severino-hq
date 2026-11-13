"""The files whose shown text still breaks a wording rule lint:prose grew, and
how many times.

Each entry is a file to rewrite. ``plain_words.held`` fails a file with more
findings of a rule than its entry, and, when lint:prose reads the whole tree,
one with fewer, whose entry must come down with the fix. So this only falls.
``mise run lint:prose`` names both; ``plain_words --whole --census`` prints
the entries the tree holds now, ``plain_words --list <paths>`` each finding
an entry counts, at its line (``uv run python -m hq.platform.core.plain_words``),
and ``mise run audit:prose-strings`` every line of text the rules read.
"""

# The rules a file may still break by its count here. Every other rule fails
# on its first finding.
BASELINED_RULES = frozenset({"Plain.Long", "Plain.Narrate", "Plain.Person", "Plain.Parts", "Label", "Kicker"})

BASELINE: dict[tuple[str, str], int] = {
    ("controller/providers/tls.go", "Plain.Long"): 1,
    ("hq/domains/control_plane/apps.py", "Plain.Parts"): 2,
    ("hq/domains/control_plane/bridge_actions.py", "Plain.Parts"): 1,
    ("hq/domains/control_plane/bridge_registry.py", "Plain.Long"): 3,
    ("hq/domains/control_plane/models.py", "Plain.Parts"): 1,
    ("hq/domains/control_plane/models.py", "Plain.Person"): 2,
    ("hq/domains/control_plane/observations/host.py", "Plain.Person"): 1,
    ("hq/domains/control_plane/provider_adapters/automation.py", "Plain.Parts"): 6,
    ("hq/domains/control_plane/provider_adapters/automation.py", "Plain.Person"): 2,
    ("hq/domains/control_plane/provider_adapters/cloudflare.py", "Plain.Long"): 1,
    ("hq/platform/application/action_links.py", "Label"): 2,
    ("hq/platform/application/adoption.py", "Plain.Long"): 1,
    ("hq/platform/application/appearance.py", "Plain.Person"): 1,
    ("hq/platform/application/automation_adoption.py", "Plain.Parts"): 6,
    ("hq/platform/application/automation_manifest.py", "Plain.Parts"): 1,
    ("hq/platform/application/automation_sources.py", "Plain.Parts"): 8,
    ("hq/platform/application/calendar_entries.py", "Plain.Person"): 1,
    ("hq/platform/application/calendar_specs.py", "Plain.Person"): 1,
    ("hq/platform/application/connection_records.py", "Plain.Long"): 3,
    ("hq/platform/application/connection_records.py", "Plain.Person"): 1,
    ("hq/platform/application/connections.py", "Plain.Person"): 1,
    ("hq/platform/application/contact_submissions.py", "Plain.Person"): 1,
    ("hq/platform/application/controller.py", "Plain.Long"): 1,
    ("hq/platform/application/controller.py", "Plain.Parts"): 7,
    ("hq/platform/application/domains.py", "Plain.Parts"): 1,
    ("hq/platform/application/domains.py", "Plain.Person"): 2,
    ("hq/platform/application/entity_links.py", "Plain.Person"): 1,
    ("hq/platform/application/findings.py", "Plain.Long"): 1,
    ("hq/platform/application/findings.py", "Plain.Person"): 1,
    ("hq/platform/application/github_findings.py", "Plain.Long"): 1,
    ("hq/platform/application/github_posture.py", "Plain.Person"): 1,
    ("hq/platform/application/mail_policy.py", "Plain.Person"): 1,
    ("hq/platform/application/pins.py", "Plain.Person"): 1,
    ("hq/platform/application/plugins.py", "Kicker"): 1,
    ("hq/platform/application/read_models.py", "Plain.Long"): 1,
    ("hq/platform/application/reboot_findings.py", "Plain.Long"): 1,
    ("hq/platform/application/references.py", "Kicker"): 1,
    ("hq/platform/application/resource_records.py", "Plain.Long"): 7,
    ("hq/platform/application/resource_records.py", "Plain.Person"): 5,
    ("hq/platform/application/resources.py", "Plain.Long"): 4,
    ("hq/platform/application/resources.py", "Plain.Person"): 1,
    ("hq/platform/application/scheduled_work.py", "Plain.Parts"): 1,
    ("hq/platform/application/tailnet_findings.py", "Plain.Person"): 1,
    ("hq/platform/application/topology_model.py", "Plain.Long"): 5,
    ("hq/platform/application/topology_model.py", "Plain.Person"): 1,
    ("hq/platform/application/work_runs.py", "Plain.Parts"): 1,
    ("hq/platform/application/workflow_contracts.py", "Plain.Long"): 1,
    ("hq/platform/application/workflow_contracts.py", "Plain.Person"): 1,
    ("hq/platform/core/audit_words.py", "Plain.Parts"): 1,
    ("hq/platform/core/example_estate/automation.py", "Plain.Parts"): 4,
    ("hq/platform/core/outbound.py", "Plain.Long"): 3,
    ("hq/platform/core/outbound.py", "Plain.Person"): 1,
    ("templates/command.html", "Label"): 2,
    ("templates/control_plane/_container_band.html", "Label"): 1,
    ("templates/control_plane/_container_detail.html", "Label"): 1,
    ("templates/control_plane/_path_hops.html", "Label"): 1,
    ("templates/control_plane/connection_list.html", "Label"): 1,
    ("templates/control_plane/container_list.html", "Label"): 1,
    ("templates/control_plane/procedures.html", "Label"): 1,
    ("templates/control_plane/service_detail.html", "Label"): 1,
    ("templates/control_plane/zone_detail.html", "Label"): 1,
    ("templates/core/_policy_rules.html", "Label"): 1,
    ("templates/partials/_connection_peering.html", "Label"): 1,
    ("templates/partials/_example_connection.html", "Label"): 1,
    ("templates/partials/_filter_bar.html", "Label"): 1,
    ("templates/partials/approval_card.html", "Label"): 1,
}
