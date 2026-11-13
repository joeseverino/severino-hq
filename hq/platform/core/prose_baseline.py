"""The files whose shown text still breaks a wording rule lint:prose grew, and
how many times.

Each entry is a file to rewrite. ``plain_words.held`` fails a file with more
findings of a rule than its entry, and, when lint:prose reads the whole tree,
one with fewer, whose entry must come down with the fix. So this only falls.
``mise run lint:prose`` names both; ``plain_words --census`` prints the
entries the tree holds now.
"""

# The rules a file may still break by its count here. Every other rule fails
# on its first finding.
BASELINED_RULES = frozenset({"Plain.Long", "Plain.Narrate"})

BASELINE: dict[tuple[str, str], int] = {
    ("controller/providers/tls.go", "Plain.Long"): 1,
    ("hq/domains/control_plane/provider_adapters/cloudflare.py", "Plain.Long"): 1,
    ("hq/domains/control_plane/provider_adapters/github.py", "Plain.Long"): 1,
    ("hq/platform/application/container_standard.py", "Plain.Long"): 1,
    ("hq/platform/application/contradiction_findings.py", "Plain.Long"): 1,
    ("hq/platform/application/documentation.py", "Plain.Long"): 1,
    ("hq/platform/application/github_findings.py", "Plain.Long"): 3,
    ("hq/platform/application/mail_policy.py", "Plain.Long"): 1,
    ("hq/platform/application/policy_fixes.py", "Plain.Long"): 1,
    ("hq/platform/application/resource_records.py", "Plain.Long"): 4,
    ("hq/platform/application/resources.py", "Plain.Long"): 5,
    ("hq/platform/application/tailnet_findings.py", "Plain.Long"): 1,
    ("hq/platform/application/topology_model.py", "Plain.Long"): 1,
    ("hq/platform/application/upgrades.py", "Plain.Long"): 1,
    ("templates/control_plane/service_list.html", "Plain.Long"): 1,
    ("templates/core/agent_policy.html", "Plain.Long"): 3,
}
